<template>
  <section class="feedback-panel">
    <h2>{{ canManage ? '问答反馈与质量改进' : '我的反馈' }}</h2>
    <p>用户反馈先待核实，不直接认定事实错误。处理结论会展示给反馈提交者，请勿写入他人隐私。</p>
    <div class="controls">
      <select v-model="filterStatus" aria-label="反馈状态"><option value="">全部状态</option>
        <option v-for="(label, value) in statuses" :value="value" :key="value">{{ label }}</option>
      </select>
      <button :disabled="busy" @click="load(1)">查询／刷新</button>
    </div>
    <p v-if="error" role="alert" class="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <div class="table-wrap"><table>
      <thead><tr><th>反馈</th><th>问题／说明</th><th>资料</th><th>状态</th><th>操作／结论</th></tr></thead>
      <tbody><tr v-for="row in rows" :key="row.id">
        <td>#{{ row.id }} · {{ categories[row.category] }}</td>
        <td>{{ row.question }}<small>{{ row.userNote || '未补充说明' }}</small></td>
        <td>{{ row.fileName }}<small>文件 #{{ row.fileId ?? '已删除' }}</small></td>
        <td>{{ statuses[row.status] }}</td>
        <td><button v-if="canManage" :disabled="busy" @click="open(row.id)">核实／处理</button><span>{{ row.resolutionNote || '待处理' }}</span></td>
      </tr></tbody>
    </table></div>
    <p v-if="!rows.length">暂无反馈。</p>
    <div class="controls"><button :disabled="busy || current <= 1" @click="load(current-1)">上一页</button>
      <span>共 {{ total }} 条 · 第 {{ current }} 页</span><button :disabled="busy || current*10 >= total" @click="load(current+1)">下一页</button></div>
    <div v-if="detail" class="modal-mask"><div class="modal-card feedback-detail" role="dialog" aria-modal="true" aria-label="反馈核实处理">
      <h2>反馈 #{{ detail.id }} · {{ statuses[detail.status] }}</h2>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <p><strong>用户问题：</strong>{{ detail.snapshot.question }}</p><p><strong>当次回答：</strong>{{ detail.snapshot.answer }}</p>
      <p><strong>用户说明：</strong>{{ detail.userNote || '无' }}</p>
      <p>原资料：{{ detail.snapshot.file.fileName }} · v{{ detail.snapshot.file.version }}；请求 {{ detail.requestId }}</p>
      <p v-if="!detail.snapshot.evidenceAvailable" class="alert">{{ detail.snapshot.evidenceNotice }}</p>
      <details><summary>当次证据与模型／检索配置（不是重新检索）</summary><pre>{{ JSON.stringify(detail.snapshot.evidence, null, 2) }}</pre></details>
      <p>回归题 #{{ detail.caseId || '未关联' }} · 目标版本文件 #{{ detail.regressionFileId || '未关联' }}</p>
      <fieldset :disabled="busy"><legend>人工核实与状态处理</legend>
        <label>问题分类<select v-model="diagnosis"><option v-for="(label, value) in diagnoses" :value="value" :key="value">{{ label }}</option></select></label>
        <label>下一状态<select v-model="nextStatus"><option v-for="value in allowedStates" :value="value" :key="value">{{ statuses[value] }}</option></select></label>
        <label>处理说明（至少5字，提交者可见）<textarea v-model="resolutionNote" maxlength="2000" /></label>
        <template v-if="nextStatus === 'closed'">
          <label>关闭结果<select v-model="resolution"><option value="explained">已解释说明</option><option value="not_issue">核实后不成立</option><option value="duplicate">重复反馈</option><option value="repaired">已修复并回归</option></select></label>
          <label v-if="resolution === 'repaired'">通过报告 ID<input v-model="reportId" placeholder="从目标资料质量评测报告复制UUID" /></label>
        </template>
        <button @click="update">保存处理结果</button>
      </fieldset>
      <fieldset v-if="canAdmin && ['in_progress','pending_regression'].includes(detail.status) && !detail.caseId" :disabled="busy">
        <legend>转为回归题（管理员先核对标准，不自动运行模型）</legend>
        <label>同一逻辑资料的未发布修复版本 ID<input v-model.number="targetFileId" type="number" min="1" /></label>
        <label>回归问题<input v-model="caseQuestion" maxlength="500" /></label>
        <label>类型<select v-model="caseKind"><option value="fact">事实题</option><option value="no_evidence">越界拒答题</option></select></label>
        <template v-if="caseKind === 'fact'"><label>应命中证据短语（每行一项）<textarea v-model="evidenceTerms" /></label><label>答案必含短语（每行一项）<textarea v-model="answerTerms" /></label></template>
        <button @click="convert">核对后添加回归题</button>
      </fieldset>
      <p>添加回归题后，到目标资料的“质量评测”启动评测；在发布修复版之前，使用有效通过报告完成修复关闭。非修复类关闭只需说明原因。</p>
      <details><summary>处理历史</summary><pre>{{ JSON.stringify(detail.events, null, 2) }}</pre></details>
      <button :disabled="busy" @click="detail = null">关闭窗口</button>
    </div></div>
  </section>
</template>
<script setup>
import { computed, onMounted, ref } from 'vue'
const props = defineProps({ request: { type: Function, required: true }, canManage: Boolean, canAdmin: Boolean })
const rows=ref([]), detail=ref(null), filterStatus=ref(''), current=ref(1), total=ref(0), busy=ref(false), error=ref(''), notice=ref('')
const diagnosis=ref('unknown'), nextStatus=ref('in_progress'), resolutionNote=ref(''), resolution=ref('explained'), reportId=ref('')
const targetFileId=ref(null), caseQuestion=ref(''), caseKind=ref('fact'), evidenceTerms=ref(''), answerTerms=ref('')
const statuses={pending:'待核实',in_progress:'处理中',pending_regression:'待回归',closed:'已关闭'}
const categories={not_helpful:'没解决问题',incomplete:'回答不完整',contradictory:'前后矛盾',unclear:'不易理解',citation_unavailable:'引用无法查看',other:'其他'}
const diagnoses={unknown:'尚未核实',knowledge:'资料缺失／过期',parse:'解析错误',retrieval:'检索漏选',generation:'回答偏离证据',interaction:'交互问题',not_issue:'反馈不成立'}
const allowedStates=computed(()=>({pending:['in_progress','closed'],in_progress:['in_progress','pending_regression','closed'],pending_regression:['pending_regression','in_progress','closed'],closed:['in_progress']}[detail.value?.status] || []))
const api=async(path,body)=>(await props.request('/customer-service/feedback/'+path,body))
const load=async(page=1)=>{
  busy.value=true; error.value=''
  try { const result=await api(props.canManage?'list':'mine',{current:page,size:10,status:filterStatus.value || null}); rows.value=result.data; total.value=result.total; current.value=result.current }
  catch(e){error.value=e.message} finally{busy.value=false}
}
const open=async id=>{
  busy.value=true; error.value=''
  try { detail.value=(await api('detail',{id})).data; diagnosis.value=detail.value.diagnosis; nextStatus.value=allowedStates.value[0]; resolutionNote.value=''; reportId.value=''; resolution.value='explained'; targetFileId.value=detail.value.regressionFileId || detail.value.fileId; caseQuestion.value=detail.value.snapshot.question; evidenceTerms.value=''; answerTerms.value='' }
  catch(e){error.value=e.message} finally{busy.value=false}
}
const update=async()=>{
  if(busy.value)return; busy.value=true; error.value=''
  try { await api('update',{id:detail.value.id,revision:detail.value.revision,status:nextStatus.value,diagnosis:diagnosis.value,note:resolutionNote.value,
    resolution:nextStatus.value==='closed'?resolution.value:null,reportId:nextStatus.value==='closed'&&resolution.value==='repaired'?reportId.value || null:null}); notice.value='处理结果已保存'; const id=detail.value.id; await open(id); await load(current.value) }
  catch(e){error.value=e.message} finally{busy.value=false}
}
const convert=async()=>{
  if(busy.value)return; busy.value=true; error.value=''
  const terms=value=>value.split('\n').map(s=>s.trim()).filter(Boolean)
  try { await api('regression-case',{id:detail.value.id,revision:detail.value.revision,targetFileId:targetFileId.value,question:caseQuestion.value,kind:caseKind.value,
    evidenceTerms:caseKind.value==='fact'?terms(evidenceTerms.value):[],answerTerms:caseKind.value==='fact'?terms(answerTerms.value):[]}); const id=detail.value.id; notice.value='已关联回归题，尚未运行评测'; await open(id); await load(current.value) }
  catch(e){error.value=e.message} finally{busy.value=false}
}
onMounted(()=>load(1))
</script>
<style scoped>
.feedback-panel{padding:24px; min-height:0; overflow:auto; flex:1}
.controls{display:flex;gap:12px;align-items:center;margin:14px 0}
small{display:block;color:#657985;margin-top:6px}
td{max-width:350px;overflow-wrap:anywhere}
.feedback-detail{width:min(960px,95vw);max-height:90vh;overflow:auto}
label{display:grid;gap:6px;margin:10px 0} input,select,textarea{width:100%;box-sizing:border-box;padding:8px}
fieldset{margin:14px 0;border:1px solid #c6d7d3;border-radius:8px;padding:14px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto;font-size:12px}
</style>
