<template>
  <details class="agent-panel" :open="Boolean(fileId)">
    <summary>资料送审预检 Agent · 检查、评测与人工确认</summary>
    <p>Agent根据工具结果选择下一步，不会自动批准、发布或代填人工审核。模型调用最多24轮，任务最长24小时。</p>
    <form class="agent-form" @submit.prevent="create">
      <label>资料ID<input v-model.number="targetId" type="number" min="1" required /></label>
      <label>任务目标<input v-model="goal" maxlength="500" required /></label>
      <label v-if="canAdmin"><input v-model="allowEvaluation" type="checkbox" />允许缺少有效报告时启动一次异步评测（会产生模型费用）</label>
      <button class="primary" :disabled="busy">启动预检</button>
      <button type="button" :disabled="busy" @click="loadTasks">恢复/刷新任务列表</button>
    </form>
    <p v-if="error" class="alert">{{ error }}</p>
    <div class="agent-history">
      <button v-for="item in tasks" :key="item.taskId" @click="select(item.taskId)">
        #{{ item.fileId }} · {{ label(item.status) }} · {{ item.taskId.slice(0, 8) }}
      </button>
    </div>
    <article v-if="task">
      <h3>资料 #{{ task.fileId }} · v{{ task.version || '未登记' }} · {{ label(task.status) }}</h3>
      <p>{{ task.goal }} · 模型调用 {{ task.steps }}/{{ task.maxSteps }} · 错误 {{ task.errors }}</p>
      <p v-if="task.current === false" class="alert">此任务绑定的资料、索引或评测配置已变化。旧报告不可用于审批，请新建预检。</p>
      <p v-if="task.stopReason" class="alert">{{ task.stopReason }}</p>
      <ul>
        <li v-for="(check, key) in task.checks" :key="key">
          <strong>{{ checkLabels[key] || key }}：{{ label(check.status) }}</strong> — {{ check.detail }}
          <pre>{{ JSON.stringify(check.evidence, null, 2) }}</pre>
        </li>
      </ul>
      <p v-if="task.status === 'waiting'">等待后台处理或评测；刷新网页不影响服务器上的任务。</p>
      <button v-if="task.status === 'awaiting_confirmation' && task.current" :disabled="busy" @click="requestSubmissionConfirmation">确认将此版本送审（不是批准或发布）</button>
      <div v-if="submissionTarget" role="alertdialog" aria-label="送审确认" class="submission-confirm">
        <p>确认将资料 #{{ submissionTarget.fileId }} · v{{ submissionTarget.version }} 的此版本送审？此操作不会批准或发布。</p>
        <button :disabled="busy || !canConfirmSubmission(task, submissionTarget)" @click="confirmSubmit">确认送审</button>
        <button :disabled="busy" @click="submissionTarget = null">暂不送审</button>
      </div>
      <p v-if="task.report">{{ task.report.recommendation }}</p>
      <p v-if="task.status === 'ready'">请回资料列表，由具备权限、非上传者的管理员使用现有审核入口确认。后端会重新执行发布门禁。</p>
      <button v-if="active.includes(task.status)" :disabled="busy" @click="cancel">取消预检（不取消已提交的独立评测）</button>
      <details><summary>工具执行记录（非模型思维过程）</summary><pre>{{ JSON.stringify(task.history, null, 2) }}</pre></details>
    </article>
  </details>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { canConfirmSubmission, mergeTaskSnapshot, createFileChangeNotifier } from './agentTask.js'
const props = defineProps({ fileId: Number, request: { type: Function, required: true }, canAdmin: Boolean })
const emit = defineEmits(['files-changed'])
const fileChanges = createFileChangeNotifier(value => emit('files-changed', value))
const targetId = ref(props.fileId || '')
const goal = ref('检查这份新资料是否具备送审条件，汇总阻塞项并生成建议')
const allowEvaluation = ref(false)
const task = ref(null)
const tasks = ref([])
const error = ref('')
const busy = ref(false)
const submissionTarget = ref(null)
const active = ['queued', 'running', 'waiting', 'awaiting_confirmation']
const checkLabels = { processing: '处理状态', integrity: '索引完整性', parse_quality: '解析质量', evaluation: '质量评测' }
const label = value => ({ queued: '排队中', running: '执行中', waiting: '等待后台任务', awaiting_confirmation: '等待送审确认',
  ready: '技术预检通过，待人工审批', blocked: '存在阻塞', incomplete: '检查未完成', stale: '版本已变化', cancelled: '已取消',
  pass: '通过', missing: '缺少有效结果' })[value] || value
let timer = null
let generation = 0
let disposed = false
watch(() => props.fileId, value => { if (value) targetId.value = value })
watch(() => [task.value?.taskId, task.value?.status, task.value?.current], () => {
  if (submissionTarget.value && !canConfirmSubmission(task.value, submissionTarget.value)) submissionTarget.value = null
})
const api = async (path, body = {}) => (await props.request('/customer-service/precheck/' + path, body)).data
const stop = () => { if (timer) clearTimeout(timer); timer = null }
const refresh = async () => {
  const id = task.value?.taskId
  const current = ++generation
  if (!id || disposed) return
  stop()
  try {
    const value = await api('detail', { taskId: id })
    if (disposed || current !== generation) return
    task.value = value
    tasks.value = mergeTaskSnapshot(tasks.value, value)
    fileChanges.observe(value)
    if (active.includes(value.status)) timer = setTimeout(refresh, 2500)
  } catch (e) { if (!disposed && current === generation) error.value = e.message }
}
const select = async id => {
  generation++; stop(); error.value = ''; task.value = { taskId: id }
  await refresh()
}
const loadTasks = async () => {
  try { const values = await api('list'); if (!disposed) tasks.value = values }
  catch (e) { if (!disposed) error.value = e.message }
}
const action = async fn => {
  busy.value = true; error.value = ''
  try { await fn() } catch (e) { if (!disposed) error.value = e.message }
  finally { busy.value = false }
}
const create = () => action(async () => {
  const value = await api('create', { id: targetId.value, goal: goal.value, allowEvaluation: allowEvaluation.value })
  if (!disposed) { await select(value.taskId); await loadTasks() }
})
const requestSubmissionConfirmation = () => {
  submissionTarget.value = { taskId: task.value.taskId, fileId: task.value.fileId, version: task.value.version }
}
const confirmSubmit = () => action(async () => {
  const target = submissionTarget.value
  if (!canConfirmSubmission(task.value, target)) throw new Error('任务或版本已变化，请重新确认')
  await api('confirm-submit', { taskId: target.taskId })
  if (!disposed) {
    fileChanges.submissionConfirmed(target)
    submissionTarget.value = null
    await refresh()
  }
})
const cancel = () => action(async () => { await api('cancel', { taskId: task.value.taskId }); await refresh(); await loadTasks() })
onMounted(loadTasks)
onBeforeUnmount(() => { disposed = true; generation++; stop() })
</script>

<style scoped>
.agent-panel { margin: 20px 0; padding: 18px; border: 1px solid #dce4ef; border-radius: 12px; background: #f8fafc; }
.agent-panel summary { cursor: pointer; font-weight: 600; }
.agent-form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.agent-form label { display: flex; gap: 6px; align-items: center; }
.agent-history { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0; }
pre { max-height: 260px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
.submission-confirm { margin: 12px 0; padding: 14px; border: 1px solid #e8b96f; border-radius: 8px; background: #fff8eb; }
</style>
