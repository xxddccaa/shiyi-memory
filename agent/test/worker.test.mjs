import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';

// Run the installed Pi core, not a reimplementation of its tool loop.
test('Pi executes only declared tools, bridges results and releases run state', {timeout: 15000}, async () => {
  const child = spawn(process.execPath, [fileURLToPath(new URL('../worker.mjs', import.meta.url))], {stdio: ['pipe', 'pipe', 'pipe']});
  const tools = [{name: 'search_memories', description: 'Search', parameters: {type: 'object', properties: {query: {type: 'string'}}, required: ['query'], additionalProperties: false}}];
  let modelCalls = 0, executed = 0;
  const send = value => child.stdin.write(JSON.stringify(value) + '\n');
  try {
    const result = await new Promise((resolve, reject) => {
      const lines = createInterface({input: child.stdout});
      child.on('error', reject);
      child.on('exit', code => {if (code) reject(new Error('worker exited'));});
      lines.on('line', line => {
        try {
          const value = JSON.parse(line);
          if (value.type === 'ready') send({type: 'start', run_id: 'test', max_model_calls: 64, context_chars: 934464, run_timeout_seconds: 86400, model_limits: {contextWindow: 1000000, maxTokens: 65536}, system_prompt: 'Test', question: 'find it', history: [], tools});
          if (value.type === 'model_request') {
            modelCalls++;
            assert.equal(value.messages[0].role, 'system');
            assert.ok(value.messages[0].content.startsWith('Test'));
            assert.ok(value.tools.some(t => t.name === 'search_memories'));
            const data = modelCalls === 1
              ? {text: '', tool_calls: [{id: 'c1', name: 'search_memories', arguments: {query: 'synthetic'}}]}
              : modelCalls === 2 ? {text: '', tool_calls: [{id: 'c2', name: 'run_shell', arguments: {command: 'forbidden'}}]}
              : {text: JSON.stringify({answer: 'from synthetic evidence', evidence_ids: ['summary:synthetic']}), tool_calls: []};
            if (modelCalls === 2) assert.ok(value.messages.some(m => m.role === 'toolResult'));
            send({type: 'reply', run_id: value.run_id, request_id: value.request_id, data});
          } else if (value.type === 'tool_request') {
            assert.equal(value.name, 'search_memories'); executed++;
            send({type: 'reply', run_id: value.run_id, request_id: value.request_id, data: {items: ['synthetic']}});
          } else if (value.type === 'result') resolve(value);
          else if (value.type === 'failed') reject(new Error('Pi failed'));
        } catch (e) {reject(e);}
      });
    });
    assert.equal(modelCalls, 3); assert.equal(executed, 1);
    assert.equal(JSON.parse(result.text).answer, 'from synthetic evidence');
  } finally {child.stdin.end(); child.kill();}
});
