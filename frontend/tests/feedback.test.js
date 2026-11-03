import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer } from 'vue'

async function component(name) {
  const { descriptor } = parse(readFileSync(new URL(`../src/${name}.vue`, import.meta.url), 'utf8'))
  let { content } = compileScript(descriptor, { id: name+'-test' })
  content = content.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  const { default: result } = await import('data:text/javascript;base64,'+Buffer.from(content).toString('base64'))
  result.render = () => null
  return result
}
const Answer = await component('AnswerFeedback'), Panel = await component('FeedbackPanel')
const renderer = createRenderer({ createElement:()=>({}),createText:()=>({}),createComment:()=>({}),
  insert(){},remove(){},setText(){},setElementText(){},patchProp(){},parentNode:()=>null,nextSibling:()=>null })
function mount(component,props) {
  const app=renderer.createApp(component,props), vm=app.mount({})
  return {app,setup:vm.$.setupState}
}
test('feedback sends only request ID, experience and optional note, with duplicate-click guard',async()=>{
  const calls=[];let done
  const {app,setup}=mount(Answer,{requestId:'test-request',request:async(path,body)=>{
    calls.push({path,body});await new Promise(resolve=>done=resolve);return {data:{id:8}}
  }})
  try {
    const first=setup.submit();await setup.submit()
    assert.equal(calls.length,1)
    assert.deepEqual(calls[0].body,{requestId:'test-request',category:'not_helpful',note:''})
    done();await first;assert.equal(setup.submitted,8);assert.equal(setup.busy,false)
    await setup.submit();assert.equal(calls.length,1)
  }finally{app.unmount()}
})
test('feedback rejected by server stays open and exposes error',async()=>{
  const {app,setup}=mount(Answer,{requestId:'r',request:async()=>{throw new Error('expired request')}})
  try {setup.open=true;await setup.submit();assert.equal(setup.submitted,null);assert.equal(setup.open,true);assert.equal(setup.error,'expired request')}
  finally{app.unmount()}
})
test('ordinary users load only their own feedback',async()=>{
  const calls=[]
  const {app}=mount(Panel,{request:async(path)=>{calls.push(path);return {data:[],total:0,current:1}}})
  try {await Promise.resolve();assert.deepEqual(calls,['/customer-service/feedback/mine'])}
  finally{app.unmount()}
})
test('manager stale revision errors do not show success or close feedback',async()=>{
  const calls=[]
  const {app,setup}=mount(Panel,{canManage:true,canAdmin:true,request:async(path,body)=>{
    calls.push({path,body})
    if(path.endsWith('/list'))return {data:[],total:0,current:1}
    if(path.endsWith('/detail'))return {data:{id:4,revision:1,status:'pending',diagnosis:'unknown',fileId:1,snapshot:{question:'test'}}}
    throw new Error('revision conflict')
  }})
  try {
    await Promise.resolve();await setup.open(4);setup.resolutionNote='verification note';await setup.update()
    assert.equal(setup.notice,'');assert.equal(setup.error,'revision conflict');assert.equal(setup.detail.status,'pending')
    assert.equal(calls.at(-1).body.revision,1)
    assert.equal(calls.some(x=>/publish|versions\/review/.test(x.path)),false)
  }finally{app.unmount()}
})
