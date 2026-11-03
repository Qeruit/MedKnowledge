import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer } from 'vue'

// Compile the actual SFC and mount its setup with Vue's custom renderer.
// No browser, backend mutation, model calls or extra test dependencies.
const source = readFileSync(new URL('../src/PrecheckPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
let { content } = compileScript(descriptor, { id: 'precheck-refresh-test' })
content = content.replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]\.\/agentTask\.js['"]/g, `from '${new URL('../src/agentTask.js', import.meta.url).href}'`)
const { default: Panel } = await import('data:text/javascript;base64,' + Buffer.from(content).toString('base64'))
Panel.render = () => null
const renderer = createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})
const snapshot = { taskId: 'test-task', fileId: 13, version: 1, current: true,
  status: 'awaiting_confirmation', pendingAction: 'submit_review' }

function mount(request, events) {
  const app = renderer.createApp(Panel, { request, canAdmin: true,
    onFilesChanged: value => events.push(value) })
  const vm = app.mount({})
  return { app, setup: vm.$.setupState }
}

test('actual panel emits a parent refresh when restoring a completed task', async () => {
  const events = []
  const { app, setup } = mount(async path => ({ data: path.endsWith('/list') ? [] : { ...snapshot, status: 'ready' } }), events)
  try {
    await setup.select('test-task')
    await setup.refresh()
    assert.deepEqual(events, [{ fileId: 13, taskId: 'test-task' }])
  } finally { app.unmount() }
})

test('actual panel emits after successful submit even if detail refresh fails', async () => {
  const events = [], calls = []
  let submitted = false
  const { app, setup } = mount(async path => {
    calls.push(path)
    if (path.endsWith('/list')) return { data: [] }
    if (path.endsWith('/confirm-submit')) { submitted = true; return { data: null } }
    if (submitted) throw new Error('synthetic detail failure')
    return { data: snapshot }
  }, events)
  try {
    await setup.select('test-task')
    events.length = 0
    setup.requestSubmissionConfirmation()
    await setup.confirmSubmit()
    assert.deepEqual(events, [{ fileId: 13, taskId: 'test-task' }])
    assert.equal(setup.error, 'synthetic detail failure')
    assert.equal(calls.filter(p => p.endsWith('/confirm-submit')).length, 1)
    assert.equal(calls.some(p => p.includes('/versions/review')), false)
  } finally { app.unmount() }
})

test('actual panel does not announce submission when confirm-submit is rejected', async () => {
  const events = []
  const { app, setup } = mount(async path => {
    if (path.endsWith('/list')) return { data: [] }
    if (path.endsWith('/confirm-submit')) throw new Error('synthetic server rejection')
    return { data: snapshot }
  }, events)
  try {
    await setup.select('test-task')
    events.length = 0
    setup.requestSubmissionConfirmation()
    await setup.confirmSubmit()
    assert.equal(events.length, 0)
    assert.equal(setup.error, 'synthetic server rejection')
  } finally { app.unmount() }
})

test('parent list subscribes to the event and preserves page/filter on refresh', () => {
  const parent = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  assert.match(parent, /@files-changed="refreshFilesAfterAgent"/)
  assert.match(parent, /const refreshFilesAfterAgent[\s\S]*?loadFiles\(pageNo\.value\)/)
})
