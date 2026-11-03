import test from 'node:test'
import assert from 'node:assert/strict'
import { canConfirmSubmission, mergeTaskSnapshot, createFileChangeNotifier } from '../src/agentTask.js'

const task = { taskId: 'task-a', fileId: 9, version: 1, status: 'awaiting_confirmation', current: true, pendingAction: 'submit_review' }
const target = { taskId: 'task-a', fileId: 9, version: 1 }
test('confirmation matches the exact current task and version', () => {
  assert.equal(canConfirmSubmission(task, target), true)
})
test('switching task, file or version invalidates the confirmation', () => {
  for (const change of [{ taskId: 'task-b' }, { fileId: 10 }, { version: 2 }]) {
    assert.equal(canConfirmSubmission({ ...task, ...change }, target), false)
  }
})
test('stale, cancelled, completed or wrong-action tasks cannot be submitted', () => {
  for (const change of [{ current: false }, { current: undefined }, { status: 'cancelled' }, { status: 'ready' }, { pendingAction: 'human_review' }]) {
    assert.equal(canConfirmSubmission({ ...task, ...change }, target), false)
  }
  assert.equal(canConfirmSubmission(null, target), false)
  assert.equal(canConfirmSubmission(task, null), false)
})
test('task-list status follows the current detail snapshot without mutating other tasks', () => {
  const first = { taskId: 'one', status: 'queued' }
  const other = { taskId: 'two', status: 'running' }
  const current = { taskId: 'one', status: 'ready' }
  const merged = mergeTaskSnapshot([first, other], current)
  assert.equal(merged[0], current)
  assert.equal(merged[1], other)
  assert.equal(first.status, 'queued')
})

test('terminal and confirmation boundaries refresh once, not every polling tick', () => {
  for (const status of ['awaiting_confirmation', 'ready', 'blocked', 'incomplete', 'stale', 'cancelled']) {
    const events = []
    const notifier = createFileChangeNotifier(value => events.push(value))
    notifier.observe({ ...task, status: 'running' })
    notifier.observe({ ...task, status })
    notifier.observe({ ...task, status })
    assert.equal(events.length, 1, status)
    assert.deepEqual(events[0], { fileId: 9, taskId: 'task-a' })
  }
})

test('restoring an already completed task synchronizes the list once', () => {
  const events = []
  const notifier = createFileChangeNotifier(value => events.push(value))
  notifier.observe({ ...task, status: 'ready' })
  notifier.observe({ ...task, status: 'ready' })
  assert.equal(events.length, 1)
})

test('successful submission notifies immediately without waiting for detail retrieval', () => {
  const events = []
  const notifier = createFileChangeNotifier(value => events.push(value))
  notifier.submissionConfirmed(target)
  assert.equal(events.length, 1)
  // Empty checkpoints and subsequent polling must not trigger redundant refreshes.
  notifier.observe({ ...task, status: 'queued' })
  notifier.observe({ ...task, status: 'running', checks: { processing: { evidence: { reviewStatus: 'pending_review' } } } })
  assert.equal(events.length, 1)
  notifier.observe({ ...task, status: 'ready' })
  assert.equal(events.length, 2)
})

test('observing server-side submission also refreshes when confirmation happened elsewhere', () => {
  const events = []
  const notifier = createFileChangeNotifier(value => events.push(value))
  notifier.observe({ ...task, status: 'running', checks: { processing: { evidence: { reviewStatus: 'draft' } } } })
  notifier.observe({ ...task, status: 'running', checks: { processing: { evidence: { reviewStatus: 'pending_review' } } } })
  notifier.observe({ ...task, status: 'waiting' })
  notifier.observe({ ...task, status: 'running', checks: { processing: { evidence: { reviewStatus: 'pending_review' } } } })
  assert.equal(events.length, 1)
})

test('task switches are isolated and malformed snapshots never notify', () => {
  const events = []
  const notifier = createFileChangeNotifier(value => events.push(value))
  for (const value of [null, {}, { taskId: 'x' }]) notifier.observe(value)
  notifier.observe({ ...task, status: 'ready' })
  notifier.observe({ ...task, taskId: 'task-b', fileId: 13, status: 'ready' })
  notifier.observe({ ...task, status: 'ready' })
  assert.deepEqual(events.map(e => e.fileId), [9, 13])
})
