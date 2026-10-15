import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {runSession, compactionSettings} from '../session.mjs';
const input = () => ({run_id: 'synthetic', conversation_id:'synthetic-conversation', max_model_calls:1024,
  model_limits:{contextWindow:1000000,maxTokens:65536}, tools:[], history:[], question:'synthetic task', system_prompt:'Application rules'});
const usage = (input=100) => ({input,output:20,cacheRead:0,cacheWrite:0,totalTokens:input+20});

test('1M context retains a meaningful recent window and reserves configured output', () => {
  assert.deepEqual(compactionSettings(1000000,65536), {enabled:true,reserveTokens:85536,keepRecentTokens:200000});
  assert.throws(()=>compactionSettings(8192,8100));
});

test('real SDK compacts through the bridge and restores the canonical entries', async () => {
  let snapshot, summaries=0, conversations=0;
  const rpc = async (type,data) => {
    if(type==='checkpoint') { snapshot=structuredClone(data); return {}; }
    if(type==='session_event') return {};
    assert.equal(type,'model_request');
    if(data.purpose==='compaction') {
      summaries++;
      assert.ok(JSON.stringify(data.messages).length < 22000, 'legacy import must not send the whole oversized transcript to the summary model');
      assert.equal(data.tools.length,0);
      assert.ok(data.messages[0].content.includes('summar'));
      assert.ok(!data.messages[0].content.includes('Application rules'));
      return {text:'## Goal\nPreserve the early requirement: synthetic-anchor.\n## Progress\nContinue the task.',usage:usage(),usage_estimated:false};
    }
    conversations++;
    return {text:'Synthetic answer',usage:usage(150),usage_estimated:false};
  };
  const first=input();
  first.model_limits={contextWindow:4096,maxTokens:512};
  first.history=Array.from({length:12},(_,i)=>({role:'user',content:`synthetic-anchor ${i}: `+'abcd '.repeat(800),timestamp:Date.now()+i}));
  assert.equal((await runSession(first,rpc,()=>{})).type,'result');
  assert.ok(summaries>0);
  assert.ok(snapshot.data.entries.some(e=>e.type==='compaction'));
  const priorCompactions=snapshot.data.entries.filter(e=>e.type==='compaction').length;
  const second={...input(),session:{data:snapshot.data,epoch:0},question:'Continue'};
  await runSession(second,async(type,data)=>{
    if(type==='model_request') {
      assert.ok(JSON.stringify(data.messages).includes('synthetic-anchor'));
      assert.ok(JSON.stringify(data.messages).includes('Synthetic answer'));
    }
    return rpc(type,data);
  },()=>{});
  assert.equal(snapshot.data.entries.filter(e=>e.type==='compaction').length,priorCompactions);
  assert.equal(conversations,2);
});

test('official file tools and project resources are journaled; global resources are absent', async () => {
  const cwd=await mkdtemp(join(tmpdir(),'pi-sdk-test-'));
  try {
    await mkdir(join(cwd,'.pi/skills/synthetic'),{recursive:true});
    await writeFile(join(cwd,'AGENTS.md'),'Project-specific synthetic instruction.');
    await writeFile(join(cwd,'.pi/skills/synthetic/SKILL.md'),'---\nname: synthetic\ndescription: Synthetic project skill\n---\nRead a synthetic file.');
    let modelCalls=0, begins=0, ends=0, checkpointed=false;
    const result=await runSession({...input(),workspace:{path:cwd,extensions:[]}},async(type,data)=>{
      if(type==='checkpoint') {checkpointed=true; return {};}
      if(type==='tool_begin') {assert.ok(checkpointed); begins++; return {};}
      if(type==='tool_end') {ends++; assert.equal(data.result.isError,undefined); return {};}
      if(type==='model_request') {
        assert.ok(data.tools.some(t=>t.name==='write'));
        assert.match(data.messages[0].content,/Project-specific synthetic instruction/);
        assert.match(data.messages[0].content,/Synthetic project skill/);
        if(modelCalls++===0) return {tool_calls:[{id:'write1',name:'write',arguments:{path:'answer.txt',content:'synthetic-success'}}],usage:usage()};
        return {text:'Done',usage:usage()};
      }
      throw new Error(type);
    },()=>{});
    assert.equal(result.type,'result'); assert.equal(begins,1); assert.equal(ends,1);
    assert.equal(await readFile(join(cwd,'answer.txt'),'utf8'),'synthetic-success');
  } finally {await rm(cwd,{recursive:true,force:true});}
});

test('only explicitly registered project extensions load and their tools are journaled', async () => {
  const cwd = await mkdtemp(join(tmpdir(), 'pi-explicit-extension-'));
  try {
    await mkdir(join(cwd, '.pi/extensions'), {recursive: true});
    await writeFile(join(cwd, '.pi/extensions/automatic.mjs'), 'throw new Error("must not automatically load project code");');
    const extension = join(cwd, 'explicit.mjs');
    await writeFile(extension, `export default function(pi) {
      pi.registerTool({name:'synthetic_extension',label:'Synthetic extension',description:'Return synthetic data',
        parameters:{type:'object',properties:{},additionalProperties:false},
        execute:async()=>({content:[{type:'text',text:'extension-success'}]})});
    }`);
    let calls = 0, starts = 0, ends = 0;
    await runSession({...input(), workspace:{path:cwd,extensions:[extension]}}, async(type,data) => {
      if(type==='checkpoint' || type==='session_event') return {};
      if(type==='tool_begin') {assert.equal(data.name,'synthetic_extension'); starts++; return {};}
      if(type==='tool_end') {assert.equal(data.result.content[0].text,'extension-success'); ends++; return {};}
      assert.equal(type,'model_request');
      assert.ok(data.tools.some(t=>t.name==='synthetic_extension'));
      return calls++ ? {text:'Done',usage:usage()} : {tool_calls:[{id:'extension-1',name:'synthetic_extension',arguments:{}}],usage:usage()};
    },()=>{});
    assert.equal(starts,1); assert.equal(ends,1);
  } finally { await rm(cwd,{recursive:true,force:true}); }
});
