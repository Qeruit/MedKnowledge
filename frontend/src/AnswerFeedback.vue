<template>
  <div class="answer-feedback">
    <button v-if="!open && !submitted" type="button" @click="open = true">反馈这条回答</button>
    <span v-if="submitted" role="status">已收到反馈 #{{ submitted }}，待核实；不代表已认定回答错误。</span>
    <form v-if="open && !submitted" @submit.prevent="submit">
      <label>遇到什么问题？<select v-model="category">
        <option value="not_helpful">没解决我的问题</option><option value="incomplete">回答不完整</option>
        <option value="contradictory">前后矛盾</option><option value="unclear">不容易理解</option>
        <option value="citation_unavailable">引用无法查看</option><option value="other">其他</option>
      </select></label>
      <label>补充说明（选填）<textarea v-model="note" maxlength="1000" placeholder="例如：只回答了温度，没有回答使用时长。请勿填写患者身份、病历等隐私。" /></label>
      <p>不需要提供正确答案，系统会保留本次回答现场，由有权限的人员核实。</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <button :disabled="busy" type="submit">{{ busy ? '提交中…' : '提交反馈' }}</button>
      <button :disabled="busy" type="button" @click="open = false">取消</button>
    </form>
  </div>
</template>
<script setup>
import { ref } from 'vue'
const props = defineProps({ requestId: { type: String, required: true }, request: { type: Function, required: true } })
const open = ref(false), submitted = ref(null), category = ref('not_helpful'), note = ref(''), error = ref(''), busy = ref(false)
const submit = async () => {
  if (busy.value || submitted.value) return
  busy.value = true; error.value = ''
  try {
    const value = await props.request('/customer-service/feedback/submit', { requestId: props.requestId, category: category.value, note: note.value })
    submitted.value = value.data.id; open.value = false
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
</script>
<style scoped>
.answer-feedback { margin-top: 12px; font-size: 12px; color: #546b77; }
form { margin-top: 8px; border: 1px solid #c9dcd7; padding: 12px; border-radius: 8px; }
label { display: grid; gap: 6px; margin-bottom: 10px; }
select, textarea { width: 100%; box-sizing: border-box; padding: 8px; }
button { margin-right: 8px; cursor: pointer; }
</style>
