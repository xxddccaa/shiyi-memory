import { createAgentSession, estimateTokens, ModelRuntime, SessionManager, SettingsManager, DefaultResourceLoader,
  createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition,
  createGrepToolDefinition, createFindToolDefinition, createLsToolDefinition, createBashToolDefinition } from '@earendil-works/pi-coding-agent';
import { estimateMessageTokens } from '@earendil-works/pi-ai/utils/estimate';
import { getCurrentSystemPrompt, getCurrentTools } from '@earendil-works/pi-ai/utils/transcript';
import { AssistantMessageEventStream } from '@earendil-works/pi-ai/utils/event-stream';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';

export function compactionSettings(contextWindow, maxTokens) {
  const reserveTokens = Math.max(maxTokens + Math.ceil(.02 * contextWindow), Math.min(16384, Math.ceil(.2 * contextWindow)));
  if (reserveTokens >= contextWindow) throw new Error('invalid_context_capacity');
  return {enabled: true, reserveTokens, keepRecentTokens: Math.min(200000, Math.floor((contextWindow - reserveTokens) / 2))};
}
const emptyUsage = () => ({input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}});

export async function runSession(input, rpc, register) {
  let events = Promise.resolve(), eventError;
  let importing = !input.session?.data && !!input.history?.length;
  let session, compacting = input.mode === 'compact', requests = 0, failure, estimated = true;
  const aborter = new AbortController();
  register({abort: async () => { aborter.abort(); await session?.abort(); }});
  const call = (type, data, signal = aborter.signal) => rpc(type, data, signal);
  const cwd = input.workspace?.path || process.cwd();
  const model = {id: 'application-model', name: input.model_name || 'Application model', api: 'openai-completions',
    provider: 'workmemory', baseUrl: 'http://application.invalid', reasoning: false, input: ['text'],
    cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}, ...input.model_limits};
  const manager = SessionManager.inMemory(cwd, {id: input.conversation_id},
    input.session?.data ? [input.session.data.header, ...input.session.data.entries] : undefined);
  if (input.session?.data) {
    if (input.session.data.leaf) manager.branch(input.session.data.leaf);
    else manager.resetLeaf();
  }
  for (const message of input.session?.recovery || []) manager.appendMessage(message);
  const checkpoint = async () => {
    await events;
    if (eventError) throw eventError;
    if (!input.conversation_id || aborter.signal.aborted || importing) return;
    await call('checkpoint', {data: {header: manager.getHeader(), entries: manager.getEntries(), leaf: manager.getLeafId()},
      context: {...session?.getContextUsage(), estimated, capacity_estimated: !!input.capacity_estimated, compacting,
        compactions: manager.getEntries().filter(e => e.type === 'compaction').length}, epoch: input.session?.epoch || 0});
  };
  const credentials = {read: async () => undefined, list: async () => [], modify: async () => undefined, delete: async () => {}};
  const modelRuntime = await ModelRuntime.create({credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false});
  modelRuntime.registerProvider('workmemory', {api: model.api, baseUrl: model.baseUrl, apiKey: 'application-bridge', authHeader: false,
    models: [model], streamSimple: (_model, context, options) => {
      const stream = new AssistantMessageEventStream();
      const message = {role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
        usage: emptyUsage(), stopReason: 'stop', timestamp: Date.now()};
      void (async () => {
        try {
          if (++requests > input.max_model_calls) throw Object.assign(new Error('budget'), {diagnostic: {error_code: 'budget_exhausted'}});
          await checkpoint();
          const result = await call('model_request', {messages: [{role: 'system', content: getCurrentSystemPrompt(context.messages)}, ...context.messages.filter(m => m.role !== 'system')],
            tools: getCurrentTools(context.messages).map(t => ({name: t.name, description: t.description, parameters: t.parameters})),
            purpose: compacting ? 'compaction' : 'conversation', max_tokens: compacting ? Math.min(16384, model.maxTokens) : model.maxTokens}, options?.signal);
          if (result.text) message.content.push({type: 'text', text: result.text});
          for (const item of result.tool_calls || []) message.content.push({type: 'toolCall', id: item.id, name: item.name, arguments: item.arguments});
          estimated = !result.usage || !!result.usage_estimated;
          if (estimated) {
            const inputTokens = context.messages.reduce((sum, m) => sum + estimateMessageTokens(m), 0);
            const outputTokens = Math.ceil(JSON.stringify(message.content).length / 4);
            message.usage = {...emptyUsage(), input: inputTokens, output: outputTokens, totalTokens: inputTokens + outputTokens};
          } else message.usage = {...emptyUsage(), ...result.usage};
          message.stopReason = result.tool_calls?.length ? 'toolUse' : 'stop';
          stream.push({type: 'start', partial: message});
          stream.push({type: 'done', reason: message.stopReason, message});
        } catch (error) {
          failure = error.diagnostic || {error_code: 'unknown', stage: compacting ? 'compaction' : 'model'};
          message.stopReason = options?.signal?.aborted ? 'aborted' : 'error';
          message.errorMessage = failure.error_code === 'context_overflow' ? 'context window exceeded' : failure.error_code;
          stream.push({type: 'error', reason: message.stopReason, error: message});
        } finally { stream.end(message); }
      })();
      return stream;
    }});
  const settingsManager = SettingsManager.inMemory({compaction: compactionSettings(model.contextWindow, model.maxTokens),
    retry: {enabled: false, maxRetries: 0, provider: {maxRetries: 0}}, cacheWarming: 'off', images: {blockImages: true}});
  const local = input.workspace;
  const resourceLoader = new DefaultResourceLoader({cwd, agentDir: join(cwd, '.workmemory-unused'), settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    additionalExtensionPaths: local?.extensions || [],
    additionalSkillPaths: local ? [join(cwd, '.pi/skills'), join(cwd, '.agents/skills')] : [],
    additionalPromptTemplatePaths: local ? [join(cwd, '.pi/prompts')] : [],
    systemPrompt: input.system_prompt,
    agentsFilesOverride: () => ({agentsFiles: []}),
  });
  await resourceLoader.reload();
  if (local) {
    let content = '';
    try { content = await readFile(join(cwd, 'AGENTS.md'), 'utf8'); } catch (e) { if (e.code !== 'ENOENT') throw e; }
    resourceLoader.getAgentsFiles = () => ({agentsFiles: content ? [{path: join(cwd, 'AGENTS.md'), content}] : []});
  }
  const appTools = input.tools.map(t => ({...t, label: t.name, execute: async (id, args, signal) => {
    const result = await call('tool_request', {call_id: id, name: t.name, arguments: args}, signal);
    return {content: [{type: 'text', text: JSON.stringify(result)}]};
  }}));
  const codingTools = local ? [createReadToolDefinition(cwd), createWriteToolDefinition(cwd), createEditToolDefinition(cwd),
    createGrepToolDefinition(cwd), createFindToolDefinition(cwd), createLsToolDefinition(cwd),
    {...createBashToolDefinition(cwd), description: '在当前项目目录执行 bash 命令。输出预览附带 output_id，可用 read_tool_output 分页读取完整输出。',
      execute: async (id, args, signal) => call('native_bash', {call_id: id, arguments: args}, signal)},
    {name: 'read_tool_output', label: '读取命令输出', description: '分页读取此前工具输出。', parameters: {type: 'object', properties: {
      output_id: {type: 'string'}, offset: {type: 'integer', minimum: 0}}, required: ['output_id'], additionalProperties: false},
      execute: async (_id, args, signal) => ({content: [{type: 'text', text: JSON.stringify(await call('output_read', args, signal))}]})},
  ] : [];
  // Tool wrappers journal before mutation and wait for the DB acknowledgement after execution.
  const wrapTool = tool => ({...tool, execute: async (id, args, signal, onUpdate, ctx) => {
    await checkpoint();
    const start = input.conversation_id ? await call('tool_begin', {call_id: id, name: tool.name, arguments: args}, signal) : {};
    if (start.result) return start.result;
    let result, before;
    const path = ['write', 'edit'].includes(tool.name) ? resolve(cwd, args.path) : null;
    if (path) { try { before = await readFile(path, 'utf8'); } catch { before = null; } }
    try { result = await tool.execute(id, args, signal, onUpdate, ctx); }
    catch (e) { if (e.diagnostic) { failure = e.diagnostic; throw e; } result = {isError: true, content: [{type: 'text', text: signal?.aborted ? '操作已取消；写入结果需要核实。' : '工具执行失败，请检查参数和当前项目状态。'}]}; }
    let change;
    if (path) { let after; try { after = await readFile(path, 'utf8'); } catch { after = null; } change = {path, before, after}; }
    if (input.conversation_id) await call('tool_end', {call_id: id, result, change}, signal);
    return result;
  }});
  const tools = [...appTools, ...codingTools].map(wrapTool);
  for (const extension of resourceLoader.getExtensions().extensions) {
    for (const registered of extension.tools.values()) registered.definition = wrapTool(registered.definition);
  }
  ({session} = await createAgentSession({cwd, agentDir: join(cwd, '.workmemory-unused'), modelRuntime, model,
    thinkingLevel: 'off', sessionManager: manager, settingsManager, resourceLoader, noTools: 'builtin', customTools: tools}));
  session.agent.toolExecution = 'sequential';
  await session.bindExtensions({});
  session.subscribe(event => {
    if (event.type === 'compaction_start') compacting = true;
    if (event.type === 'compaction_end') compacting = false;
    if (['compaction_start', 'compaction_end'].includes(event.type)) {
      events = events.then(() => call('session_event', {kind: event.type, reason: event.reason, failed: !!event.errorMessage}))
        .catch(error => { eventError = error; });
    }
  });
  try {
    const settings = compactionSettings(model.contextWindow, model.maxTokens);
    let compacted = false;
    if (importing) {
      // Incremental legacy import keeps the summarizer's own input within a useful
      // window. No partial import checkpoint is published: a crash restarts this
      // read-only conversion from PostgreSQL, so no trailing history can disappear.
      const target = Math.floor((model.contextWindow - settings.reserveTokens) * .7);
      const chunkChars = Math.max(256, target);
      for (const original of input.history) {
        const text = typeof original.content === 'string' ? original.content : original.content.map(c => c.text || '').join('\n');
        for (let offset = 0; offset < text.length; offset += chunkChars) {
          const part = text.slice(offset, offset + chunkChars);
          manager.appendMessage({...original, content: original.role === 'assistant' ? [{type:'text',text:part}] : part});
          session.agent.state.messages = manager.buildSessionContext().messages;
          const tokens = manager.buildSessionContext().messages.reduce((sum, m) => sum + (m.role === 'system' ? 0 : estimateTokens(m)), 0);
          if (tokens > target) {
            try { await session.compact(); compacted = true; }
            catch (e) {
              if (!['Already compacted', 'Nothing to compact (session too small)'].includes(e.message)) {
                throw Object.assign(e, {diagnostic: failure || {error_code:'compaction_failed',stage:'compaction'}});
              }
            }
          }
        }
      }
      importing = false;
      await checkpoint();
    }
    if (input.mode === 'compact' || (session.getContextUsage()?.tokens || 0) > model.contextWindow - settings.reserveTokens) {
      try { await session.compact(); compacted = true; }
      catch (e) {
        if (!['Already compacted', 'Nothing to compact (session too small)'].includes(e.message)) {
          throw Object.assign(e, {diagnostic: failure || {error_code: 'compaction_failed', stage: 'compaction'}});
        }
      }
    }
    if (input.mode !== 'compact') await session.prompt(input.question, {source: 'rpc'});
    await checkpoint();
    if (input.mode === 'compact') return {type: 'result', text: '', compacted};
    const last = session.messages.filter(m => m.role === 'assistant').at(-1);
    if (!last || ['error', 'aborted', 'toolUse'].includes(last.stopReason)) return {type: 'failed', diagnostic: failure || {error_code: 'unknown', stage: 'agent'}};
    return {type: 'result', text: last.content.filter(c => c.type === 'text').map(c => c.text).join('\n')};
  } finally { session.dispose(); }
}
