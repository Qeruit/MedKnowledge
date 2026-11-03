// A confirmation belongs to exactly one currently actionable task/version view.
// The server independently checks actor, fingerprint, expiry and permissions.
export function canConfirmSubmission(task, target) {
  return Boolean(task && target && task.taskId === target.taskId && task.fileId === target.fileId &&
    task.version === target.version && task.status === 'awaiting_confirmation' && task.current === true &&
    task.pendingAction === 'submit_review')
}

export function mergeTaskSnapshot(tasks, snapshot) {
  return tasks.map(item => item.taskId === snapshot.taskId ? snapshot : item)
}

// Observe authoritative API snapshots, not every polling tick. Never performs
// a submission or an approval; the callback only refreshes the parent list.
export function createFileChangeNotifier(notify) {
  const seen = new Map()
  const boundaries = new Set(['awaiting_confirmation', 'ready', 'blocked', 'incomplete', 'stale', 'cancelled'])
  return {
    observe(snapshot) {
      if (!snapshot?.taskId || !snapshot.fileId || !snapshot.status) return
      const previous = seen.get(snapshot.taskId)
      const reviewStatus = snapshot.checks?.processing?.evidence?.reviewStatus
      const changedBoundary = boundaries.has(snapshot.status) &&
        (!previous || previous.status !== snapshot.status || previous.current !== snapshot.current)
      const submitted = reviewStatus === 'pending_review' && previous?.reviewStatus !== 'pending_review'
      seen.set(snapshot.taskId, {
        status: snapshot.status, current: snapshot.current,
        reviewStatus: reviewStatus ?? previous?.reviewStatus,
      })
      if (changedBoundary || submitted) notify({ fileId: snapshot.fileId, taskId: snapshot.taskId })
    },
    submissionConfirmed(target) {
      if (!target?.taskId || !target.fileId) return
      const previous = seen.get(target.taskId) || {}
      // Only called AFTER the confirm-submit HTTP request succeeds, even if
      // the subsequent detail request fails or the user has selected another task.
      seen.set(target.taskId, { ...previous, status: 'queued', reviewStatus: 'pending_review' })
      notify({ fileId: target.fileId, taskId: target.taskId })
    },
  }
}
