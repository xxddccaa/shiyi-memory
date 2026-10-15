// Python owns persistence, credentials and native command processes.
import {runSession} from './session.mjs';
import {createInterface} from 'node:readline';
// Extensions share this process; keep their console diagnostics off the JSONL channel.
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'dir', 'trace']) console[method] = () => {};
const runs = new Map(), pending = new Map();
let sequence = 0;
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
function rpc(runId, kind, data, signal, timeoutMs) {
  return new Promise((resolve, reject) => {
    const requestId = String(++sequence);
    let timer;
    const cleanup = () => { clearTimeout(timer); signal?.removeEventListener('abort', stop); };
    const stop = () => {
      cleanup();
      pending.delete(requestId);
      reject(new Error('cancelled'));
    };
    if (signal?.aborted) return stop();
    timer = setTimeout(() => {
      cleanup(); pending.delete(requestId);
      reject(Object.assign(new Error('Request timed out'), {
        diagnostic: {error_code: 'model_timeout', stage: kind === 'tool_request' ? 'tool' : 'model'},
      }));
    }, timeoutMs);
    signal?.addEventListener('abort', stop, {once: true});
    pending.set(requestId, {runId, resolve: v => {cleanup(); resolve(v);}, reject: e => {cleanup(); reject(e);}});
    send({type: kind, run_id: runId, request_id: requestId, ...data});
  });
}

async function start(input) {
  if (runs.has(input.run_id)) return;
  runs.set(input.run_id, {abort() {}});
  try {
    const result = await runSession(input,
      (type, data, signal) => rpc(input.run_id, type, data, signal, input.run_timeout_seconds * 1000),
      session => runs.set(input.run_id, session));
    send({...result, run_id: input.run_id});
  } catch (error) {
    send({type: 'failed', run_id: input.run_id, diagnostic: error.diagnostic || {error_code: error.message === 'invalid_context_capacity' ? 'invalid_context_capacity' : 'unknown', stage: 'agent'}});
  } finally {
    runs.delete(input.run_id);
    for (const [id, item] of pending) if (item.runId === input.run_id) { item.reject(new Error('finished')); pending.delete(id); }
  }
}
const input = createInterface({input: process.stdin, crlfDelay: Infinity});
input.on('line', line => {
  try {
    const value = JSON.parse(line);
    if (value.type === 'start') void start(value);
    else if (value.type === 'cancel') void runs.get(value.run_id)?.abort();
    else if (value.type === 'reply') {
      const item = pending.get(value.request_id);
      if (item && item.runId === value.run_id) {
        pending.delete(value.request_id);
        if (value.error) item.reject(Object.assign(new Error('Backend request failed'), {diagnostic: value.error}));
        else item.resolve(value.data);
      }
    }
  } catch { /* Never log prompts, outputs or provider errors. */ }
});
input.on('close', () => { for (const session of runs.values()) void session.abort(); process.exit(0); });
send({type: 'ready', protocol: 2});
