<template>
  <main v-if="!user" class="login-screen">
    <form class="login-card" @submit.prevent="login">
      <h1>MedKnowledge</h1><p>登录后查询或维护获授权的资料</p>
      <label>用户名<input v-model="loginForm.username" autocomplete="username" required /></label>
      <label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password" required /></label>
      <p v-if="authError" class="alert">{{ authError }}</p>
      <button class="primary" :disabled="authLoading">{{ authLoading ? '正在验证…' : '登录' }}</button>
    </form>
  </main>
  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">✦</span><span>MedKnowledge</span></div>
      <p class="brand-subtitle">医疗资料知识库问答与管理系统</p>
      <nav class="navigation" aria-label="主导航">
        <button :class="{ active: tab === 'chat' }" @click="openChat">资料问答</button>
        <button v-if="canManage" :class="{ active: tab === 'admin' }" @click="openAdmin">知识库管理</button>
        <button :class="{ active: tab === 'feedback' }" @click="tab = 'feedback'">{{ canManage ? '问答反馈' : '我的反馈' }}</button>
        <button v-if="canAdmin" :class="{ active: tab === 'users' }" @click="openUsers">账号管理</button>
        <button v-if="canAdmin" :class="{ active: tab === 'ops' }" @click="openOperations">运行监控</button>
      </nav>
      <div class="side-section">
        <span class="eyebrow">建议试问</span>
        <button v-for="q in examples" :key="q" class="example" @click="useExample(q)">{{ q }}</button>
      </div>
      <div class="side-footer">
        <span class="status-dot" :class="{ healthy: ready }"></span>
        {{ ready ? '本地服务已就绪' : '正在检查服务' }}
        <p>演示资料来自公开说明书；回答不构成诊断、治疗或器械使用建议。</p>
      </div>
    </aside>

    <main class="main-panel">
      <header class="topbar">
        <div>
          <span class="eyebrow">DOCUMENT-GROUNDED ASSISTANT</span>
          <h1>{{ tab === 'chat' ? '资料问答' : tab === 'feedback' ? '问答反馈' : tab === 'users' ? '账号管理' : tab === 'ops' ? '运行监控' : '知识库管理' }}</h1>
        </div>
        <div class="account-bar"><span>{{ user.username }} · {{ roleText(user.role) }}</span>
          <button @click="passwordDialog = true">修改密码</button><button @click="logout">退出</button>
        </div>
      </header>
      <div v-if="adminError || reviewNotice" class="review-feedback" :class="{ 'review-feedback-error': adminError }" role="alert">
        {{ adminError || reviewNotice }}
        <button @click="adminError = ''; reviewNotice = ''" aria-label="关闭操作提示">×</button>
      </div>

      <section v-if="tab === 'chat'" class="chat-screen">
        <div class="document-scope">
          <label for="chat-document">本次问答资料</label>
          <select id="chat-document" v-model.number="selectedFileId" :disabled="pending || !availableDocuments.length"
                  @change="handleDocumentChange">
            <option value="" disabled>请选择一份已发布资料</option>
            <option v-for="doc in availableDocuments" :key="doc.id" :value="doc.id">
              {{ doc.fileName }}{{ doc.documentKey ? ` · v${doc.version}` : '' }}
            </option>
          </select>
          <button type="button" class="new-conversation" :disabled="pending || !selectedFileId" @click="startNewConversation">新建会话</button>
          <span v-if="!availableDocuments.length">当前没有已发布且获授权的资料，请联系系统管理员。</span>
          <span v-else>追问会参考本会话的提问；事实仅从所选资料检索。切换资料将新建会话。</span>
        </div>
        <div ref="chatScroll" class="chat-scroll">
          <div class="chat-inner">
            <div v-for="(item, index) in messages" :key="index" class="message-row" :class="item.role">
              <div v-if="item.role === 'assistant'" class="avatar">✦</div>
              <div class="bubble">
                <div v-if="item.loading && !item.content" class="thinking">正在检索资料并生成回答<span class="dots">…</span></div>
                <div v-else-if="item.role === 'assistant'" class="markdown" v-html="renderMarkdown(item.content)"></div>
                <div v-else class="user-text">{{ item.content }}</div>
                <details v-if="item.role === 'assistant' && (item.sources?.length || (item.completed && item.requestId))" class="sources answer-details">
                  <summary>{{ item.sources?.length ? '本次检索证据与回答反馈' : '回答反馈' }}</summary>
                  <div v-for="(source, i) in item.sources" :key="i" class="source">
                    <strong>{{ source.heading }}</strong>
                    <span>{{ source.citation }}</span>
                    <span v-if="source.documentKey">资料版本 v{{ source.version }} · {{ source.documentKey }}</span>
                    <a v-if="source.sourceUrl?.startsWith('https://') || source.sourceUrl?.startsWith('http://')"
                       :href="source.sourceUrl" target="_blank" rel="noopener noreferrer">查看原始来源 ↗</a>
                  </div>
                  <AnswerFeedback v-if="item.completed && item.requestId" :key="item.requestId" :request-id="item.requestId" :request="adminFetch" />
                </details>
              </div>
            </div>
          </div>
        </div>
        <form class="composer" @submit.prevent="send">
          <textarea v-model="draft" rows="2" maxlength="2000" placeholder="询问医疗器械说明书中的事实，例如：设备的存储温度是多少？"
                    @keydown.enter.exact.prevent="send"></textarea>
          <div class="composer-bottom">
            <span>仅依据已入库资料回答；请核对原始说明书。</span>
            <button type="submit" class="primary" :disabled="pending || !draft.trim() || !selectedFileId">{{ pending ? '回答中…' : '发送提问 ↗' }}</button>
          </div>
        </form>
      </section>

      <FeedbackPanel v-else-if="tab === 'feedback'" :key="user.id" :request="adminFetch" :can-manage="canManage" :can-admin="canAdmin" />
      <section v-else-if="tab === 'admin' && canManage" class="admin-screen">
        <div class="admin-intro">
          <div><h2>资料生命周期</h2><p>上传、向量化、检索与审计均使用独立数据库，不影响通用聊天项目。</p></div>
          <span class="pill">{{ roleText(user.role) }}</span>
        </div>
        <div class="token-panel"><button @click="loadFiles(pageNo)">刷新资料</button>
          <span>{{ canAdmin ? '可管理全部资料及授权' : '仅管理自己上传的资料' }}</span>
        </div>
        <p v-if="adminError" class="alert">{{ adminError }}</p>
        <p v-if="versionPolicy.allowSelfApproval" class="alert">本地演示：允许单管理员例外审批。仍需逐页验收和有效质量评测，审批原因会记录为非独立审核。正式部署请关闭此模式。</p>
        <PrecheckPanel :key="user.id" :file-id="precheckFileId" :request="adminFetch" :can-admin="canAdmin" @files-changed="refreshFilesAfterAgent" />
        <p v-if="stats.workersAlive === 0" class="alert">当前未检测到 Worker 心跳，上传任务会保留在队列中。请启动独立 Worker。</p>
        <p class="format-hint">任务队列：等待 {{ stats.tasksByStatus?.queued ?? 0 }} · 执行中 {{ stats.tasksByStatus?.running ?? 0 }} · 失败 {{ stats.tasksByStatus?.failed ?? 0 }} · 活跃 Worker {{ stats.workersAlive ?? 0 }}</p>
        <div class="stats">
          <div><span>资料总数</span><strong>{{ total }}</strong></div>
          <div><span>已完成</span><strong>{{ stats.filesByStatus?.['2'] ?? 0 }}</strong></div>
          <div><span>处理中</span><strong>{{ (stats.filesByStatus?.['0'] ?? 0) + (stats.filesByStatus?.['1'] ?? 0) }}</strong></div>
          <div><span>向量片段</span><strong>{{ stats.vectorCount ?? 0 }}</strong></div>
        </div>
        <div class="toolbar">
          <div class="filters">
            <input v-model="filter.fileName" placeholder="按文件名搜索" @keydown.enter="loadFiles(1)" />
            <select v-model="filter.reviewStatus" aria-label="审核状态筛选">
              <option value="">全部审核状态</option><option value="pending_review">待审核</option>
              <option value="draft">草稿</option><option value="approved">审核通过</option>
              <option value="rejected">已驳回</option><option value="superseded">历史版本</option>
            </select>
            <input v-model="filter.startDate" type="date" title="开始日期" />
            <input v-model="filter.endDate" type="date" title="结束日期" />
            <button @click="loadFiles(1)">查询</button>
            <button class="subtle" @click="resetFilters">重置</button>
          </div>
          <label class="upload-button" :class="{ disabled: uploading }">
            {{ uploading ? '上传中…' : '＋ 上传问答文件' }}
            <input type="file" :accept="fileAccept" :disabled="uploading" @change="uploadFile" />
          </label>
        </div>
        <p class="format-hint">支持 {{ supportedExtensions.join('、').toUpperCase() }}；扫描 PDF 请先 OCR 后上传。</p>
        <p class="format-hint">版本发布流程：上传 → 登记版本 → 向量化 → 送审 → 标注题库并运行质量评测 → 由另一位管理员批准。评测失败或报告过期时禁止发布。</p>
        <div v-if="uploadState.stage" class="upload-progress" role="status">
          <div><strong>{{ uploadState.fileName }}</strong><span>{{ uploadState.stage }} · {{ uploadState.percent }}%</span></div>
          <progress :value="uploadState.percent" max="100"></progress>
          <p>支持 SHA-256 秒传、分片校验及断点续传。页面刷新后重新选择同一文件即可续传。</p>
          <button v-if="uploading" @click="pauseUpload">暂停上传</button>
          <button v-else-if="uploadState.stage === '已暂停' || uploadState.stage === '上传失败'" @click="resumeUpload">继续上传</button>
        </div>
        <p class="document-table-hint" id="document-table-help">表格可左右滚动，右侧操作栏固定显示；长备注可展开查看。</p>
        <div class="table-wrap documents-table-wrap" role="region" aria-label="问答文件列表" aria-describedby="document-table-help" tabindex="0">
          <table class="document-table">
            <colgroup>
              <col class="document-col-name" /><col class="document-col-size" /><col class="document-col-status" />
              <col class="document-col-access" /><col class="document-col-date" /><col class="document-col-remark" /><col class="document-col-actions" />
            </colgroup>
            <thead><tr><th>文件名称</th><th>大小</th><th>状态</th><th>访问状态</th><th>创建时间</th><th>备注</th><th class="document-actions-cell">操作</th></tr></thead>
            <tbody>
              <tr v-for="file in files" :key="file.id">
                <td><strong>{{ file.originalFileName }}</strong><small>#{{ file.id }}<template v-if="file.documentKey"> · v{{ file.version }} · {{ file.revisionLabel }}</template></small></td>
                <td>{{ file.fileSize }}</td>
                <td><span class="status" :class="'status-' + file.status">{{ statusText(file.status) }}</span>
                  <small v-if="file.task">任务：{{ ({queued:'等待执行／重试',running:'执行中',succeeded:'成功',failed:'失败'})[file.task.status] }} · 尝试 {{ file.task.attempts }}/{{ file.task.maxAttempts }}</small>
                  <small v-if="file.processingError" class="processing-error">{{ file.processingError }}</small>
                  <details v-if="file.processingWarnings?.length"><summary>解析说明</summary>
                    <small v-for="note in file.processingWarnings" :key="note">{{ note }}</small>
                  </details>
                </td>
                <td><span>{{ file.isPublished ? '当前发布' : reviewStatusText(file.reviewStatus) }}</span><small>负责人：{{ file.ownerName || '未分配' }}</small></td>
                <td class="document-date">{{ file.createTime?.replace('T', ' ') }}</td>
                <td class="remark">
                  <span class="remark-preview">{{ file.remark || '—' }}</span>
                  <details v-if="file.remark" class="remark-details"><summary>查看完整备注</summary><p>{{ file.remark }}</p></details>
                </td>
                <td class="actions document-actions-cell">
                  <small class="action-file-id">资料 #{{ file.id }}</small>
                  <div class="document-actions">
                  <button @click="editRemark(file)">编辑</button>
                  <button @click="precheckFileId = file.id">Agent预检</button>
                  <button v-if="!file.documentKey" @click="openRegisterVersion(file)">登记版本</button>
                  <button v-if="file.documentKey && file.status === 2 && ['draft','rejected'].includes(file.reviewStatus)" @click="submitVersion(file)">送审</button>
                  <button v-if="file.documentKey" @click="openQuality(file)">质量评测</button>
                  <button v-if="file.originalFileName?.toLowerCase().endsWith('.pdf') && file.status === 2" class="document-audit-action" @click="openPdfAudit(file)">PDF 逐页验收</button>
                  <button v-if="canAdmin && file.reviewStatus === 'pending_review'" @click="reviewVersion(file, true)">批准</button>
                  <button v-if="canAdmin && file.reviewStatus === 'pending_review'" @click="reviewVersion(file, false)">驳回</button>
                  <button v-if="file.documentKey" @click="showVersionHistory(file)">版本历史</button>
                  <button v-if="!file.documentKey && file.isPublished" @click="togglePublish(file)">撤回旧版</button>
                  <button v-if="canAdmin && (file.isPublished || !file.documentKey)" @click="openGrant(file)">授权</button>
                  <button v-if="file.status === 2 && (!file.documentKey || ['draft','rejected'].includes(file.reviewStatus))" @click="reindexFile(file)">重新索引</button>
                  <button v-if="file.status === 3" @click="retryFile(file)">重试</button>
                  <button v-if="!file.documentKey" class="danger" :disabled="file.status < 2" @click="deleteFile(file)">删除</button>
                  </div>
                </td>
              </tr>
              <tr v-if="!files.length"><td colspan="7" class="empty">{{ loading ? '正在加载…' : '暂无资料，上传问答文件开始构建知识库。' }}</td></tr>
            </tbody>
          </table>
        </div>
        <div class="pagination">
          <span>共 {{ total }} 条 · 第 {{ pageNo }} / {{ Math.max(1, Math.ceil(total / pageSize)) }} 页</span>
          <div><button :disabled="pageNo <= 1" @click="loadFiles(pageNo - 1)">上一页</button><button :disabled="pageNo * pageSize >= total" @click="loadFiles(pageNo + 1)">下一页</button></div>
        </div>
        <details class="audit">
          <summary @click="loadAudit">最近管理操作审计</summary>
          <p v-if="!audits.length">暂无操作记录。</p>
          <div v-for="event in audits" :key="event.id" class="audit-row">
            <span>{{ event.createTime?.replace('T', ' ') }}</span>
            <strong>{{ event.action }}</strong>
            <span>{{ event.actorName }}</span><span v-if="event.fileId">资料 #{{ event.fileId }}</span>
            <span>{{ event.detail }}</span>
          </div>
        </details>
      </section>
      <section v-else-if="tab === 'ops' && canAdmin" class="admin-screen">
        <h2>运行监控与备份</h2><p>最近 {{ operations.windowSeconds || 300 }} 秒的指标，5 秒自动刷新。指标不记录问题、回答或密码。</p>
        <p>记录时间按数据库时区 {{ operations.databaseTimezone || '服务器时区' }} 展示。</p>
        <button @click="loadOperations">刷新</button><p v-if="adminError" class="alert">{{ adminError }}</p>
        <p v-if="operations.agentAlive === false" class="alert">监控 Agent 心跳缺失，请启动 python -m app.ops_agent；未运行时不会自动评估告警或备份。</p>
        <div class="stats"><div><span>活跃 Worker</span><strong>{{ operations.workersAlive ?? '—' }}</strong></div>
          <div><span>等待任务</span><strong>{{ operations.queue?.queued ?? 0 }}</strong></div>
          <div><span>最老排队 / 秒</span><strong>{{ Math.round(operations.oldestQueuedSeconds || 0) }}</strong></div>
          <div><span>模型失败 / 调用</span><strong>{{ operations.modelErrors ?? 0 }} / {{ operations.modelCalls ?? 0 }}</strong></div></div>
        <h3>告警状态</h3><div v-for="alert in operations.alerts || []" :key="alert.rule" class="ops-alert" :class="{alert:alert.state === 'firing'}">
          <strong>{{ alert.rule }} · {{ alert.state }}</strong><span>{{ alert.detail }}</span></div>
        <h3>请求与模型指标</h3><div class="table-wrap"><table><thead><tr><th>类别</th><th>结果</th><th>窗口次数</th><th>耗时 P50 / 秒</th><th>耗时 P95 / 秒</th><th>首个回答 P95 / 秒</th></tr></thead><tbody>
          <tr v-for="row in operations.events || []" :key="row.kind + row.outcome"><td>{{ row.kind }}</td><td>{{ row.outcome }}</td><td>{{ row.count }}</td><td>{{ row.p50?.toFixed(3) }}</td><td>{{ row.p95?.toFixed(3) }}</td><td>{{ row.ttft?.toFixed(3) || '—' }}</td></tr></tbody></table></div>
        <h3>问答用量与额度</h3><p>日／月额度分别按 UTC 计算。Token 为本地估算；费用仅在后端配置模型费率后才有参考意义，并非供应商账单。</p>
        <p>每人日额度 {{ chatUsage.dailyTokenLimit ?? '—' }} Token；月额度 {{ chatUsage.monthlyTokenLimit ?? '—' }} Token。</p>
        <div class="table-wrap"><table><thead><tr><th>用户</th><th>今日请求</th><th>今日 Token</th><th>本月 Token</th><th>本月估算费用</th><th>活跃问答</th></tr></thead><tbody>
          <tr v-for="row in chatUsage.users || []" :key="row.userId"><td>{{ row.username }}</td><td>{{ row.requestsToday }}</td><td>{{ row.tokensToday }}</td><td>{{ row.tokensMonth }}</td><td>{{ row.estimatedCostMonth }}</td><td>{{ row.active }}</td></tr></tbody></table></div>
        <h3>备份记录</h3><p>备份包含数据库、原始资料和未完成分片。归档含敏感数据，仅限本机管理员维护，不提供公开下载。</p>
        <div class="table-wrap"><table><thead><tr><th>备份 ID</th><th>状态</th><th>开始时间</th><th>耗时 / 秒</th><th>字节数</th><th>文件数</th></tr></thead><tbody>
          <tr v-for="backup in operations.backups || []" :key="backup.id"><td>{{ backup.id }}</td><td>{{ backup.status }}</td><td>{{ backup.started_at }}</td><td>{{ backup.duration?.toFixed(3) || '—' }}</td><td>{{ backup.bytes ?? '—' }}</td><td>{{ backup.file_count ?? '—' }}</td></tr></tbody></table></div>
        <h3>告警触发／解除历史</h3><div v-for="entry in operations.alertHistory || []" :key="entry.id" class="audit-row"><span>{{ entry.create_time }}</span><strong>{{ entry.rule }} · {{ entry.state }}</strong><span>{{ entry.detail }}</span></div>
      </section>
      <section v-else-if="tab === 'users' && canAdmin" class="admin-screen">
        <h2>账号与角色</h2><p>查询者仅查询授权资料；资料管理员维护自己的文件；系统管理员管理账号及授权。</p>
        <p v-if="adminError" class="alert">{{ adminError }}</p>
        <form class="user-create" @submit.prevent="createAccount">
          <input v-model="newUser.username" placeholder="用户名（英文小写、数字）" autocomplete="off" required />
          <input v-model="newUser.password" type="password" placeholder="初始密码（至少 12 字符）" autocomplete="new-password" minlength="12" required />
          <select v-model="newUser.role"><option value="viewer">查询者</option><option value="editor">资料管理员</option><option value="admin">系统管理员</option></select>
          <button class="primary">创建账号</button>
        </form>
        <div class="table-wrap"><table><thead><tr><th>用户名</th><th>角色</th><th>启用</th><th>操作</th></tr></thead><tbody>
          <tr v-for="account in accounts" :key="account.id"><td>{{ account.username }}</td>
            <td><select v-model="account.role" :disabled="account.id === user.id"><option value="viewer">查询者</option><option value="editor">资料管理员</option><option value="admin">系统管理员</option></select></td>
            <td><input v-model="account.isActive" type="checkbox" :disabled="account.id === user.id" /></td>
            <td><button :disabled="account.id === user.id" @click="saveAccount(account)">保存权限</button>
              <button v-if="account.id !== user.id" @click="resetAccount = account; resetPassword = ''">重置密码</button></td>
          </tr></tbody></table></div>
      </section>
    </main>
    <div v-if="grantFile" class="modal-mask"><form class="modal-card" @submit.prevent="saveGrant">
      <h2>授权：{{ grantFile.originalFileName }}</h2><p>授权用户可查询已发布的资料。负责人和系统管理员自带访问权限。</p>
      <label v-for="account in accounts.filter(a => a.isActive && a.role !== 'admin' && a.id !== grantFile.ownerId)" :key="account.id" class="grant-choice">
        <input v-model="grantUserIds" type="checkbox" :value="account.id" />{{ account.username }} · {{ roleText(account.role) }}
      </label>
      <p v-if="adminError" class="alert">{{ adminError }}</p>
      <button class="primary">保存授权</button><button type="button" @click="grantFile = null">取消</button>
    </form></div>
    <div v-if="versionFile" class="modal-mask"><form class="modal-card" @submit.prevent="registerVersion">
      <h2>登记资料版本 · #{{ versionFile.id }}</h2>
      <p>首次登记旧资料请留空旧版本 ID；上传新文件作为修订版时，填写同一负责人已有版本的文件 ID。</p>
      <label>旧版本文件 ID（可选）<input v-model="versionForm.baseId" type="number" min="1" placeholder="留空则登记为 v1" /></label>
      <label>修订标识<input v-model="versionForm.revisionLabel" maxlength="100" placeholder="例如 2026-10 修订版" required /></label>
      <label>原始来源 URL（可选）<input v-model="versionForm.sourceUrl" type="url" placeholder="https://…" /></label>
      <p v-if="adminError" class="alert">{{ adminError }}</p>
      <button class="primary">确认登记</button><button type="button" @click="versionFile = null">取消</button>
    </form></div>
    <div v-if="historyDocument" class="modal-mask"><div class="modal-card version-history">
      <h2>版本历史 · {{ historyDocument.documentKey }}</h2>
      <p>旧版保留用于追溯；只有当前发布版本参与问答检索。</p>
      <div v-for="entry in versionHistory" :key="entry.id" class="audit-row">
        <strong>v{{ entry.version }} · #{{ entry.id }}</strong><span>{{ entry.fileName }}</span>
        <span>{{ reviewStatusText(entry.reviewStatus) }}{{ entry.isPublished ? ' · 当前发布' : '' }}</span>
        <span>{{ entry.revisionLabel }} · SHA-256 {{ entry.sha256?.slice(0, 12) || '—' }}</span>
        <span>Embedding：{{ entry.embeddingModel || '历史记录未采集' }}</span>
        <span v-if="entry.reviewNote">{{ entry.reviewNote }}</span>
        <button v-if="canAdmin && entry.reviewStatus === 'superseded' && entry.status === 2" @click="rollbackVersion(entry)">回滚到此版本</button>
      </div>
      <p v-if="adminError" class="alert">{{ adminError }}</p>
      <button @click="historyDocument = null">关闭</button>
    </div></div>
    <div v-if="qualityFile" class="modal-mask"><div class="modal-card quality-dialog">
      <h2>发布前质量评测 · v{{ qualityFile.version }} · #{{ qualityFile.id }}</h2>
      <p>标注题绑定逻辑文档；候选版与当前发布版分别检索、回答。全部标注题通过且报告未过期，才能批准发布。回答事实采用文字片段核验，仍需人工复核语义。</p>
      <p v-if="qualityTask">评测任务 {{ qualityTask.jobId }} · {{ ({queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败'})[qualityTask.status] }} · {{ qualityTask.cursor }}/{{ qualityTask.total }} 题 · 尝试 {{ qualityTask.attempts }}/{{ qualityTask.maxAttempts }}</p>
      <p v-if="qualityTask?.error" class="alert">{{ qualityTask.error }}</p>
      <div v-if="canAdmin" class="quality-form">
        <label>问题<input v-model="qualityForm.question" maxlength="500" placeholder="例如：设备建议的存储温度是多少？" /></label>
        <label>类型<select v-model="qualityForm.kind"><option value="fact">事实题</option><option value="no_evidence">应拒答</option></select></label>
        <label v-if="qualityForm.kind === 'fact'">应命中证据词（每行一项）<textarea v-model="qualityForm.evidenceTerms" placeholder="说明书中的章节或事实短语" /></label>
        <label v-if="qualityForm.kind === 'fact'">回答必须包含（每行一项）<textarea v-model="qualityForm.answerTerms" placeholder="稳定的关键事实短语，不要用易变措辞" /></label>
        <label class="grant-choice"><input v-model="qualityForm.critical" type="checkbox" />关键题</label>
        <button :disabled="qualityBusy" @click="addQualityCase">添加标注题</button>
      </div>
      <h3>标注题（{{ qualityCases.length }}）</h3>
      <div v-for="item in qualityCases" :key="item.id" class="audit-row">
        <strong>#{{ item.id }} · {{ item.kind === 'fact' ? '事实题' : '拒答题' }}{{ item.critical ? ' · 关键' : '' }}</strong>
        <span>{{ item.question }}</span><small v-if="item.kind === 'fact'">证据：{{ item.evidenceTerms.join('、') }}；回答：{{ item.answerTerms.join('、') }}</small>
        <button v-if="canAdmin" :disabled="qualityBusy" @click="deleteQualityCase(item)">删除题目</button>
      </div>
      <button v-if="canAdmin && qualityFile.reviewStatus === 'pending_review'" :disabled="qualityBusy || !qualityCases.length" @click="runQuality">运行新旧版本评测</button>
      <p v-if="qualityReport" :class="qualityReport.current && qualityReport.passed ? 'quality-pass' : 'alert'">
        {{ qualityReport.current ? '当前报告' : '报告已过期，须重跑' }} · {{ qualityReport.passed ? '全部通过' : '未通过' }} · {{ qualityReport.total }} 题 / {{ qualityReport.failed }} 失败 / {{ qualityReport.regressions }} 退化
      </p>
      <p v-if="qualityReport?.runId">评测报告 ID：<code>{{ qualityReport.runId }}</code>（反馈回归关闭时使用）</p>
      <div v-for="item in qualityReport?.cases || []" :key="item.case.id" class="quality-result">
        <strong>{{ item.candidate.passed ? '通过' : '失败' }}{{ item.regression ? ' · 相比旧版退化' : '' }} · {{ item.case.question }}</strong>
        <details><summary>当前发布版 · {{ item.baseline ? (item.baseline.passed ? '通过' : '失败') : '无基线' }}</summary>
          <template v-if="item.baseline"><p>回答：{{ item.baseline.answer }}</p><p v-for="(e, i) in item.baseline.evidence" :key="i">证据 {{ i+1 }}：{{ e.content }}</p></template>
        </details>
        <details open><summary>候选版 · {{ item.candidate.passed ? '通过' : '失败' }}</summary>
          <p>回答：{{ item.candidate.answer }}</p><p v-for="(e, i) in item.candidate.evidence" :key="i">证据 {{ i+1 }}：{{ e.content }}</p>
          <small>核验：{{ JSON.stringify(item.candidate.checks) }}</small>
        </details>
      </div>
      <p v-if="adminError" class="alert">{{ adminError }}</p>
      <button @click="closeQuality">关闭（任务会继续执行）</button>
    </div></div>
    <div v-if="pdfAuditFile" class="modal-mask"><div class="modal-card pdf-audit-dialog">
      <h2>PDF 逐页解析验收 · #{{ pdfAuditFile.id }}</h2>
      <p>对照原 PDF、抽取文本和实际向量片段。自动通过仅表示未触发版式风险规则，不代表医学内容已审核；待复核或排除页面不会参与问答检索。</p>
      <p v-if="pdfAudit">共 {{ pdfAudit.pageCount }} 页 · 已记录 {{ pdfAudit.auditedPages }} 页 · 待复核 {{ pdfAudit.needsReview }} 页 · 排除 {{ pdfAudit.excluded }} 页 · {{ pdfAudit.ready ? '满足页级发布门禁' : '页级验收未完成' }}</p>
      <button v-if="pdfAudit && !pdfAudit.auditedPages" :disabled="pdfAuditBusy" @click="generatePdfAudit">为现有索引生成逐页报告（不重新向量化）</button>
      <div v-if="pdfAudit?.auditedPages" class="pdf-audit-layout">
        <div class="pdf-page-list">
          <label class="pdf-filter"><input v-model="pdfRiskOnly" type="checkbox" />仅看风险页</label>
          <button v-for="item in pdfAudit.pages.filter(p => !pdfRiskOnly || p.status === 'needs_review' || p.flags.length)" :key="item.pageNumber"
            :class="{ active: pdfPage?.pageNumber === item.pageNumber }" @click="loadPdfPage(item.pageNumber)">
            PDF {{ item.pageNumber }} 页 · {{ ({auto_ok:'自动通过',needs_review:'待复核',approved:'人工通过',excluded:'排除'})[item.status] }}
            <small>{{ item.extractedChars }} 字 · {{ item.indexedChunks }} 段 · {{ item.flags.join('、') }}</small>
          </button>
        </div>
        <div v-if="pdfPage" class="pdf-page-detail">
          <h3>PDF 第 {{ pdfPage.pageNumber }} 页 · {{ pdfPage.flags.join('、') || '未触发风险规则' }}</h3>
          <div class="pdf-compare">
            <section><h4>原始 PDF 页面</h4><img :src="`/api/customer-service/md/pdf/preview/${pdfAuditFile.id}/${pdfPage.pageNumber}`" alt="原 PDF 页面预览" /></section>
            <section><h4>解析文本</h4><pre>{{ pdfPage.extractedText || '无可提取文本' }}</pre><h4>实际索引片段（{{ pdfPage.indexedChunks.length }}）</h4>
              <details v-for="(chunk, index) in pdfPage.indexedChunks" :key="index"><summary>片段 {{ index + 1 }}</summary><pre>{{ chunk }}</pre></details>
            </section>
          </div>
          <label>人工复核记录（至少 5 字）<textarea v-model="pdfReviewNote" placeholder="写明与原页核对的结论、图表限制或排除原因" /></label>
          <button :disabled="pdfAuditBusy" @click="reviewPdfPage('approved')">确认抽取和索引可用</button>
          <button :disabled="pdfAuditBusy" @click="reviewPdfPage('excluded')">排除该页证据</button>
          <p v-if="pdfPage.note">上次结论：{{ pdfPage.note }}</p>
        </div>
      </div>
      <p v-if="adminError" class="alert">{{ adminError }}</p>
      <button @click="closePdfAudit">关闭</button>
    </div></div>
    <div v-if="passwordDialog || resetAccount" class="modal-mask"><form class="modal-card" @submit.prevent="submitPassword">
      <h2>{{ resetAccount ? '重置 ' + resetAccount.username + ' 的密码' : '修改密码' }}</h2>
      <label v-if="!resetAccount">原密码<input v-model="oldPassword" type="password" autocomplete="current-password" required /></label>
      <label>新密码<input v-model="resetPassword" type="password" autocomplete="new-password" minlength="12" required /></label>
      <p v-if="adminError" class="alert">{{ adminError }}</p><button class="primary">保存</button>
      <button type="button" @click="passwordDialog = false; resetAccount = null; oldPassword = ''; resetPassword = ''">取消</button>
    </form></div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import PrecheckPanel from './PrecheckPanel.vue'
import AnswerFeedback from './AnswerFeedback.vue'
import FeedbackPanel from './FeedbackPanel.vue'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import MarkdownIt from 'markdown-it'
import { createSHA256 } from 'hash-wasm'
import { filePageQuery } from './fileQuery.js'

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
const renderMarkdown = value => markdown.render(value || '')
const examples = [
  'ActaStim-S 的存储温度是多少？治疗需要多久？',
  '出现橙色回路未闭合提示应检查什么？',
  '孕妇使用 ActaStim-S 是否已被证实安全？',
]
const tab = ref('chat')
const user = ref(null)
const canManage = computed(() => ['editor', 'admin'].includes(user.value?.role))
const canAdmin = computed(() => user.value?.role === 'admin')
const roleText = role => ({ viewer: '查询者', editor: '资料管理员', admin: '系统管理员' })[role] || role
const loginForm = reactive({ username: '', password: '' })
const authLoading = ref(true)
const authError = ref('')
const availableDocuments = ref([])
const selectedFileId = ref('')
const conversationId = ref(null)
const accounts = ref([])
const newUser = reactive({ username: '', password: '', role: 'viewer' })
const passwordDialog = ref(false)
const resetAccount = ref(null)
const oldPassword = ref('')
const resetPassword = ref('')
const grantFile = ref(null)
const grantUserIds = ref([])
const versionFile = ref(null)
const versionForm = reactive({ baseId: '', revisionLabel: '', sourceUrl: '' })
const historyDocument = ref(null)
const versionHistory = ref([])
const qualityFile = ref(null)
const precheckFileId = ref(null)
const qualityCases = ref([])
const qualityReport = ref(null)
const qualityTask = ref(null)
const qualityBusy = ref(false)
let qualityPollTimer = null
const qualityForm = reactive({ question: '', kind: 'fact', evidenceTerms: '', answerTerms: '', critical: true })
const pdfAuditFile = ref(null)
const pdfAudit = ref(null)
const pdfPage = ref(null)
const pdfReviewNote = ref('')
const pdfRiskOnly = ref(true)
const pdfAuditBusy = ref(false)
const ready = ref(false)
const draft = ref('')
const pending = ref(false)
const chatScroll = ref(null)
const messages = ref([{ role: 'assistant', content: '你好！我可以帮你查询已入库医疗器械资料中的事实，并展示检索依据。请勿将回答作为诊断或治疗建议。' }])
let chatController = null
const chatStorageKey = () => user.value ? `medknowledge-chat-${user.value.id}` : null
const forgetSavedConversation = () => {
  try { if (chatStorageKey()) sessionStorage.removeItem(chatStorageKey()) } catch { /* Storage can be disabled. */ }
}
const rememberConversation = () => {
  try {
    if (chatStorageKey() && conversationId.value && selectedFileId.value) {
      sessionStorage.setItem(chatStorageKey(), JSON.stringify({ fileId: selectedFileId.value, conversationId: conversationId.value }))
    }
  } catch { /* The server remains authoritative when browser storage is unavailable. */ }
}

const scrollBottom = async () => {
  await nextTick()
  if (chatScroll.value) chatScroll.value.scrollTop = chatScroll.value.scrollHeight
}
const useExample = question => { tab.value = 'chat'; draft.value = question }
const checkReady = async () => {
  try { ready.value = (await fetch('/api/ready')).ok } catch { ready.value = false }
}
const send = async () => {
  const question = draft.value.trim()
  const fileId = selectedFileId.value
  const currentConversationId = conversationId.value
  const userId = user.value?.id
  if (!question || !fileId || pending.value || !user.value) return
  pending.value = true
  draft.value = ''
  messages.value.push({ role: 'user', content: question })
  const answer = reactive({ role: 'assistant', content: '', loading: true, sources: [] })
  messages.value.push(answer)
  await scrollBottom()
  const controller = new AbortController()
  chatController = controller
  let closed = false
  try {
    await fetchEventSource('/api/customer-service/completion', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': user.value.csrfToken },
      body: JSON.stringify({ message: question, fileId, conversationId: currentConversationId }),
      signal: controller.signal,
      openWhenHidden: true,
      async onopen(response) {
        if (response.status === 401) { clearSession(); throw new Error('登录已过期，请重新登录') }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(typeof body.detail === 'string' ? body.detail : `问答接口返回 HTTP ${response.status}`)
        }
        if (!response.headers.get('content-type')?.includes('text/event-stream')) {
          throw new Error('问答接口未返回 SSE 流，请检查后端服务。')
        }
      },
      onmessage(event) {
        if (event.event === 'conversation') {
          const scope = JSON.parse(event.data)
          answer.requestId = scope.requestId
          if (user.value?.id === userId && selectedFileId.value === scope.fileId) {
            conversationId.value = scope.id
            rememberConversation()
          }
        } else if (event.event === 'sources') {
          answer.sources = JSON.parse(event.data).items || []
        } else if (event.event === 'close') {
          closed = true
          answer.completed = true
          controller.abort()
        } else if (event.event === 'error') {
          throw new Error(JSON.parse(event.data).message || '回答生成失败')
        } else if (!event.event && event.data) {
          answer.loading = false
          answer.content += JSON.parse(event.data).v || ''
          scrollBottom()
        }
      },
      onclose() { if (!closed) throw new Error('连接意外中断') },
      onerror(error) { throw error },
    })
  } catch (error) {
    if (!closed) {
      answer.content += (answer.content ? '\n\n' : '') + '请求失败：' + error.message
    }
    if (error.message.includes('资料不存在、未发布或无查询权限')) {
      loadAvailable().catch(() => {})
    } else if (error.message.includes('会话不存在') || error.message.includes('会话已过期') ||
               error.message.includes('会话属于另一份资料')) {
      conversationId.value = null
      forgetSavedConversation()
    }
  } finally {
    answer.loading = false
    pending.value = false
    if (chatController === controller) chatController = null
    scrollBottom()
  }
}

const adminError = ref('')
const reviewNotice = ref('')
const versionPolicy = ref({ allowSelfApproval: false })
const reviewBusy = ref(false)
const files = ref([])
const audits = ref([])
const total = ref(0)
const pageNo = ref(1)
const pageSize = 10
const loading = ref(false)
const uploading = ref(false)
const uploadState = reactive({ fileName: '', stage: '', percent: 0 })
let selectedUploadFile = null
let uploadController = null
const stats = ref({})
const operations = ref({})
const chatUsage = ref({})
const loadOperations = async () => {
  const identity = user.value?.id
  try {
    const [data, usage] = await Promise.all([adminGet('/ops/status'), adminGet('/ops/chat-usage')])
    if (user.value?.id === identity) { operations.value = data; chatUsage.value = usage; adminError.value = '' }
  } catch (error) { adminError.value = error.message }
}
const openOperations = async () => { tab.value = 'ops'; await loadOperations() }
const supportedExtensions = ref(['md', 'markdown', 'pdf', 'docx', 'txt'])
const fileAccept = computed(() => supportedExtensions.value.map(ext => '.' + ext).join(','))
const filter = reactive({ fileName: '', reviewStatus: '', startDate: '', endDate: '' })
let pollTimer = null
let agentFileRefreshTimer = null
let filesRequestGeneration = 0

const adminFetch = async (path, body, form = false) => {
  if (!user.value) throw new Error('请先登录')
  const response = await fetch('/api' + path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-CSRF-Token': user.value.csrfToken, ...(form ? {} : { 'Content-Type': 'application/json' }) },
    body: form ? body : JSON.stringify(body),
  })
  if (response.status === 401) { clearSession(); throw new Error('登录已过期，请重新登录') }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === 'string' ? error.detail : '服务请求失败：HTTP ' + response.status)
  }
  const data = await response.json()
  if (!data.success) throw new Error(data.message || '操作失败')
  return data
}
const adminGet = async path => {
  if (!user.value) throw new Error('请先登录')
  const response = await fetch('/api' + path, { credentials: 'include' })
  if (response.status === 401) { clearSession(); throw new Error('登录已过期，请重新登录') }
  if (!response.ok) throw new Error('服务请求失败：HTTP ' + response.status)
  return (await response.json()).data
}
const clearSession = () => {
  filesRequestGeneration++
  window.clearTimeout(agentFileRefreshTimer); agentFileRefreshTimer = null
  reviewNotice.value = ''; versionPolicy.value = { allowSelfApproval: false }
  chatController?.abort(); uploadController?.abort()
  forgetSavedConversation()
  user.value = null
  tab.value = 'chat'
  draft.value = ''; files.value = []; audits.value = []; accounts.value = []
  availableDocuments.value = []
  selectedFileId.value = ''
  conversationId.value = null
  total.value = 0; pageNo.value = 1; stats.value = {}; adminError.value = ''
  operations.value = {}; chatUsage.value = {}
  filter.fileName = ''; filter.reviewStatus = ''; filter.startDate = ''; filter.endDate = ''
  messages.value = [{ role: 'assistant', content: '你好！我可以查询你获授权的已发布资料，并展示检索依据。' }]
  selectedUploadFile = null; uploadState.stage = ''; uploadState.fileName = ''; uploadState.percent = 0
  grantFile.value = null; passwordDialog.value = false; resetAccount.value = null
  versionFile.value = null; historyDocument.value = null; versionHistory.value = []
  closeQuality()
  precheckFileId.value = null
  oldPassword.value = ''; resetPassword.value = ''; newUser.password = ''; newUser.username = ''
}
const loadAvailable = async () => {
  const id = user.value?.id
  const data = await adminGet('/customer-service/md/available')
  if (user.value?.id !== id) return
  availableDocuments.value = data
  if (!data.some(doc => doc.id === selectedFileId.value)) {
    selectedFileId.value = data.length === 1 ? data[0].id : ''
    resetScopedChat()
    try {
      const saved = JSON.parse(sessionStorage.getItem(chatStorageKey()) || 'null')
      if (saved && !data.some(doc => doc.id === saved.fileId)) forgetSavedConversation()
    } catch { forgetSavedConversation() }
  }
}
const resetScopedChat = () => {
  conversationId.value = null
  messages.value = [{ role: 'assistant', content: '你好！请选择资料后提问；我只会检索当前选中的已发布资料，并展示检索依据。请勿将回答作为诊断或治疗建议。' }]
}
const startNewConversation = () => { forgetSavedConversation(); resetScopedChat() }
const handleDocumentChange = () => { startNewConversation() }
const restoreChat = async () => {
  let saved
  try { saved = JSON.parse(sessionStorage.getItem(chatStorageKey()) || 'null') } catch { return }
  if (!saved?.conversationId || !availableDocuments.value.some(doc => doc.id === saved.fileId)) return
  try {
    const response = await fetch(`/api/customer-service/conversations/${encodeURIComponent(saved.conversationId)}`, {
      credentials: 'include',
    })
    if (!response.ok) throw new Error('会话无法恢复')
    const data = (await response.json()).data
    if (user.value && data.fileId === saved.fileId) {
      selectedFileId.value = data.fileId
      conversationId.value = data.id
      messages.value = [messages.value[0], ...data.turns.flatMap(turn => [
        { role: 'user', content: turn.question },
        { role: 'assistant', content: turn.answer, sources: turn.sources || [], requestId: turn.requestId, completed: true },
      ])]
      scrollBottom()
    }
  } catch { forgetSavedConversation(); resetScopedChat() }
}
const restoreSession = async () => {
  try {
    const response = await fetch('/api/auth/me', { credentials: 'include' })
    if (response.ok) { user.value = (await response.json()).data; await loadAvailable(); await restoreChat() }
  } catch { authError.value = '连接登录服务失败，请检查后端' }
  finally { authLoading.value = false }
}
const login = async () => {
  authLoading.value = true; authError.value = ''
  try {
    const response = await fetch('/api/auth/login', { method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: loginForm.username.trim().toLowerCase(), password: loginForm.password }) })
    const result = await response.json()
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请检查用户名格式及密码')
    user.value = result.data
    await loadAvailable()
    await restoreChat()
  } catch (error) { authError.value = error.message }
  finally { loginForm.password = ''; authLoading.value = false }
}
const logout = async () => {
  try { await adminFetch('/auth/logout', {}) } catch { /* Local view is cleared even if session already expired. */ }
  clearSession()
}
const openChat = async () => {
  tab.value = 'chat'
  try { await loadAvailable() } catch (error) { adminError.value = error.message }
}
const loadAccounts = async () => {
  const id = user.value?.id
  const data = await adminGet('/admin/users')
  if (user.value?.id === id) accounts.value = data
}
const openUsers = async () => {
  tab.value = 'users'; adminError.value = ''
  try { await loadAccounts() } catch (error) { adminError.value = error.message }
}
const createAccount = async () => {
  adminError.value = ''
  try {
    await adminFetch('/admin/users', { ...newUser, username: newUser.username.trim().toLowerCase() })
    newUser.username = ''; newUser.password = ''; newUser.role = 'viewer'
    await loadAccounts()
  } catch (error) { adminError.value = error.message }
}
const saveAccount = async account => {
  adminError.value = ''
  try { await adminFetch('/admin/users/update', { id: account.id, role: account.role, isActive: account.isActive }); await loadAccounts() }
  catch (error) { adminError.value = error.message }
}
const submitPassword = async () => {
  adminError.value = ''
  try {
    if (resetAccount.value) {
      const id = resetAccount.value.id
      await loadAccounts()
      const account = accounts.value.find(a => a.id === id)
      await adminFetch('/admin/users/update', { id, role: account.role, isActive: account.isActive, password: resetPassword.value })
      resetAccount.value = null; resetPassword.value = ''; await loadAccounts()
    } else {
      await adminFetch('/auth/password', { oldPassword: oldPassword.value, newPassword: resetPassword.value })
      clearSession(); authError.value = '密码已修改，请使用新密码登录'
    }
  } catch (error) { adminError.value = error.message }
}
const togglePublish = async file => {
  try {
    await adminFetch('/customer-service/md/publish', { id: file.id, isPublished: !file.isPublished })
    await loadFiles(pageNo.value)
  } catch (error) { adminError.value = error.message }
}
const reviewStatusText = status => ({
  draft: '草稿', pending_review: '待审核', approved: '审核通过',
  rejected: '已驳回', superseded: '历史版本',
})[status] || '未登记版本'
const openRegisterVersion = file => {
  adminError.value = ''
  versionFile.value = file
  versionForm.baseId = ''
  versionForm.revisionLabel = ''
  versionForm.sourceUrl = ''
}
const registerVersion = async () => {
  if (!versionFile.value) return
  try {
    await adminFetch('/customer-service/md/versions/register', {
      id: versionFile.value.id,
      baseId: versionForm.baseId ? Number(versionForm.baseId) : null,
      revisionLabel: versionForm.revisionLabel.trim(),
      sourceUrl: versionForm.sourceUrl.trim() || null,
    })
    versionFile.value = null
    await loadFiles(pageNo.value)
  } catch (error) { adminError.value = error.message }
}
const submitVersion = async file => {
  if (!window.confirm(`提交 v${file.version} 审核？提交后不得原地重新索引或删除。`)) return
  try { await adminFetch('/customer-service/md/versions/submit', { id: file.id }); await loadFiles(pageNo.value) }
  catch (error) { adminError.value = error.message }
}
const openQuality = async file => {
  stopQualityPolling()
  adminError.value = ''
  qualityFile.value = file
  qualityCases.value = []
  qualityReport.value = null
  qualityTask.value = null
  try {
    qualityCases.value = (await adminFetch('/customer-service/md/quality/cases', { documentKey: file.documentKey })).data
    qualityReport.value = (await adminFetch('/customer-service/md/quality/latest', { id: file.id })).data
    await refreshQualityStatus()
  } catch (error) { adminError.value = error.message }
}
const stopQualityPolling = () => {
  if (qualityPollTimer) window.clearInterval(qualityPollTimer)
  qualityPollTimer = null
}
const closeQuality = () => {
  stopQualityPolling()
  qualityFile.value = null; qualityTask.value = null; qualityBusy.value = false
}
const refreshQualityStatus = async () => {
  const fileId = qualityFile.value?.id
  if (!fileId) return
  try {
    const task = (await adminFetch('/customer-service/md/quality/status', { id: fileId })).data
    if (qualityFile.value?.id !== fileId) return
    const previousStatus = qualityTask.value?.status
    qualityTask.value = task
    qualityBusy.value = ['queued', 'running'].includes(task?.status)
    if (qualityBusy.value && !qualityPollTimer) {
      qualityPollTimer = window.setInterval(refreshQualityStatus, 2000)
    } else if (!qualityBusy.value) {
      stopQualityPolling()
      if (task?.status === 'succeeded' && previousStatus !== 'succeeded') {
        qualityReport.value = (await adminFetch('/customer-service/md/quality/latest', { id: fileId })).data
      }
    }
  } catch (error) { stopQualityPolling(); qualityBusy.value = false; adminError.value = error.message }
}
const addQualityCase = async () => {
  if (!qualityFile.value) return
  try {
    await adminFetch('/customer-service/md/quality/cases/add', {
      documentKey: qualityFile.value.documentKey,
      question: qualityForm.question.trim(), kind: qualityForm.kind,
      evidenceTerms: qualityForm.kind === 'fact' ? qualityForm.evidenceTerms.split('\n').map(x => x.trim()).filter(Boolean) : [],
      answerTerms: qualityForm.kind === 'fact' ? qualityForm.answerTerms.split('\n').map(x => x.trim()).filter(Boolean) : [],
      critical: qualityForm.critical,
    })
    qualityForm.question = ''; qualityForm.evidenceTerms = ''; qualityForm.answerTerms = ''
    await openQuality(qualityFile.value)
  } catch (error) { adminError.value = error.message }
}
const deleteQualityCase = async item => {
  if (!window.confirm(`删除评测题 #${item.id}？已有报告会失效。`)) return
  try { await adminFetch('/customer-service/md/quality/cases/delete', { id: item.id }); await openQuality(qualityFile.value) }
  catch (error) { adminError.value = error.message }
}
const runQuality = async () => {
  qualityBusy.value = true; adminError.value = ''
  try {
    await adminFetch('/customer-service/md/quality/run', { id: qualityFile.value.id })
    await refreshQualityStatus()
  } catch (error) { adminError.value = error.message }
  finally { if (!qualityTask.value || !['queued', 'running'].includes(qualityTask.value.status)) qualityBusy.value = false }
}
const reviewVersion = async (file, approve) => {
  if (reviewBusy.value) return
  adminError.value = ''; reviewNotice.value = ''
  const selfApproval = approve && file.ownerId === user.value.id
  if (selfApproval && !versionPolicy.value.allowSelfApproval) {
    adminError.value = '审批人与资料上传者必须是不同账号；本地演示例外审批未开启。'
    return
  }
  const note = window.prompt(selfApproval ? '本地演示单管理员例外审批：请填写至少5字的原因（非独立审核）' : approve ? '审核意见（可选）' : '请填写驳回原因（必填）', '')
  if (note === null || (!approve && !note.trim())) return
  if (selfApproval && note.trim().length < 5) { adminError.value = '例外审批原因至少填写5个字。'; return }
  if (approve && !window.confirm(`${selfApproval ? '本地演示例外审批（非独立审核）：' : ''}批准 v${file.version} 并设为当前发布版本？旧版将立即退出检索。`)) return
  reviewBusy.value = true
  try {
    await adminFetch('/customer-service/md/versions/review', { id: file.id, approve, note, localDemoSelfApproval: selfApproval })
    reviewNotice.value = approve ? `资料 #${file.id} v${file.version} 已批准发布${selfApproval ? '（本地演示单管理员例外）' : ''}。` : '已驳回该版本。'
    await loadFiles(pageNo.value); await loadAvailable()
  }
  catch (error) { adminError.value = error.message }
  finally { reviewBusy.value = false }
}
const showVersionHistory = async file => {
  try {
    const result = await adminFetch('/customer-service/md/versions/history', { documentKey: file.documentKey })
    versionHistory.value = result.data
    historyDocument.value = file
  } catch (error) { adminError.value = error.message }
}
const rollbackVersion = async entry => {
  const reason = window.prompt(`回滚到 v${entry.version} 的原因（必填）`, '')
  if (!reason?.trim()) return
  if (!window.confirm('确认将旧版重新设为当前发布版本？此操作将立即改变问答检索范围。')) return
  try {
    await adminFetch('/customer-service/md/versions/rollback', { id: entry.id, reason })
    const key = historyDocument.value.documentKey
    versionHistory.value = (await adminFetch('/customer-service/md/versions/history', { documentKey: key })).data
    await loadFiles(pageNo.value)
    await loadAvailable()
  } catch (error) { adminError.value = error.message }
}
const openGrant = async file => {
  adminError.value = ''
  try { await loadAccounts(); grantUserIds.value = [...file.grantedUserIds]; grantFile.value = file }
  catch (error) { adminError.value = error.message }
}
const saveGrant = async () => {
  try {
    await adminFetch('/customer-service/md/grants', { id: grantFile.value.id, userIds: grantUserIds.value })
    grantFile.value = null; await loadFiles(pageNo.value)
  } catch (error) { adminError.value = error.message }
}
const openAdmin = () => { if (canManage.value) { tab.value = 'admin'; loadFiles(1) } }
const loadFiles = async page => {
  const generation = ++filesRequestGeneration
  const id = user.value?.id
  loading.value = true
  adminError.value = ''
  try {
    const result = await adminFetch('/customer-service/md/list', filePageQuery(page, pageSize, filter))
    if (user.value?.id !== id || generation !== filesRequestGeneration) return
    files.value = result.data || []
    total.value = result.total || 0
    pageNo.value = result.current || page
    const statistics = await adminGet('/customer-service/md/stats')
    const formats = await adminGet('/customer-service/md/formats')
    const policy = await adminGet('/customer-service/md/versions/policy')
    if (user.value?.id === id && generation === filesRequestGeneration) { stats.value = statistics; supportedExtensions.value = formats.extensions; versionPolicy.value = policy }
  } catch (error) {
    if (user.value?.id === id && generation === filesRequestGeneration) adminError.value = error.message
  } finally { if (generation === filesRequestGeneration) loading.value = false }
}
const refreshFilesAfterAgent = () => {
  const identity = user.value?.id
  if (!identity || tab.value !== 'admin' || !canManage.value) return
  window.clearTimeout(agentFileRefreshTimer)
  agentFileRefreshTimer = window.setTimeout(() => {
    agentFileRefreshTimer = null
    if (user.value?.id === identity && tab.value === 'admin' && canManage.value) loadFiles(pageNo.value)
  }, 150)
}
const resetFilters = () => {
  filter.fileName = ''; filter.reviewStatus = ''; filter.startDate = ''; filter.endDate = ''; loadFiles(1)
}
const closePdfAudit = () => {
  pdfAuditFile.value = null; pdfAudit.value = null; pdfPage.value = null; pdfReviewNote.value = ''
}
const openPdfAudit = async file => {
  pdfAuditFile.value = file; pdfPage.value = null; pdfAudit.value = null; adminError.value = ''
  try {
    pdfAudit.value = (await adminFetch('/customer-service/md/pdf/pages', { id: file.id })).data
    const first = pdfAudit.value.pages.find(item => item.status === 'needs_review') || pdfAudit.value.pages[0]
    if (first) await loadPdfPage(first.pageNumber)
  } catch (error) { adminError.value = error.message }
}
const generatePdfAudit = async () => {
  if (!pdfAuditFile.value || pdfAuditBusy.value) return
  pdfAuditBusy.value = true; adminError.value = ''
  try {
    pdfAudit.value = (await adminFetch('/customer-service/md/pdf/audit', { id: pdfAuditFile.value.id })).data
    const first = pdfAudit.value.pages.find(item => item.status === 'needs_review') || pdfAudit.value.pages[0]
    if (first) await loadPdfPage(first.pageNumber)
  } catch (error) { adminError.value = error.message }
  finally { pdfAuditBusy.value = false }
}
const loadPdfPage = async pageNumber => {
  if (!pdfAuditFile.value) return
  adminError.value = ''
  try {
    pdfPage.value = (await adminFetch('/customer-service/md/pdf/page', {
      id: pdfAuditFile.value.id, pageNumber,
    })).data
    pdfReviewNote.value = pdfPage.value.note || ''
  } catch (error) { adminError.value = error.message }
}
const reviewPdfPage = async decision => {
  if (!pdfAuditFile.value || !pdfPage.value || pdfAuditBusy.value) return
  pdfAuditBusy.value = true; adminError.value = ''
  try {
    const pageNumber = pdfPage.value.pageNumber
    await adminFetch('/customer-service/md/pdf/review', {
      id: pdfAuditFile.value.id, pageNumber, decision, note: pdfReviewNote.value.trim(),
    })
    pdfAudit.value = (await adminFetch('/customer-service/md/pdf/pages', { id: pdfAuditFile.value.id })).data
    await loadPdfPage(pageNumber)
  } catch (error) { adminError.value = error.message }
  finally { pdfAuditBusy.value = false }
}
const statusText = code => ({ 0: '待处理', 1: '向量化中', 2: '已完成', 3: '失败' })[code] || '未知'
const UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
const MAX_UPLOAD_SIZE = 256 * 1024 * 1024
const pauseUpload = () => { uploadController?.abort() }
const resumeUpload = () => { if (selectedUploadFile) startUpload(selectedUploadFile) }
const uploadFile = event => {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  selectedUploadFile = file
  startUpload(file)
}
const sha256Hex = async buffer => {
  const result = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(result), byte => byte.toString(16).padStart(2, '0')).join('')
}
const hashFile = async (file, signal) => {
  const hasher = await createSHA256()
  hasher.init()
  for (let offset = 0; offset < file.size; offset += UPLOAD_CHUNK_SIZE) {
    if (signal.aborted) throw new DOMException('上传已暂停', 'AbortError')
    hasher.update(new Uint8Array(await file.slice(offset, offset + UPLOAD_CHUNK_SIZE).arrayBuffer()))
    uploadState.percent = Math.min(10, Math.round(10 * Math.min(offset + UPLOAD_CHUNK_SIZE, file.size) / file.size))
    await new Promise(resolve => setTimeout(resolve, 0))
  }
  return hasher.digest('hex')
}
const uploadPart = async (uploadId, index, chunkSize, file, signal) => {
  const blob = file.slice(index * chunkSize, Math.min((index + 1) * chunkSize, file.size))
  const checksum = await sha256Hex(await blob.arrayBuffer())
  for (let attempt = 0; attempt < 3; attempt++) {
    if (signal.aborted) throw new DOMException('上传已暂停', 'AbortError')
    try {
      const response = await fetch(`/api/customer-service/md/uploads/${uploadId}/chunks/${index}`, {
        method: 'PUT', signal, credentials: 'include', headers: {
          'X-CSRF-Token': user.value?.csrfToken || '',
          'X-Chunk-Sha256': checksum,
          'Content-Type': 'application/octet-stream',
        }, body: blob,
      })
      if (!response.ok) throw new Error(`分片 ${index + 1} 上传失败：HTTP ${response.status}`)
      const result = await response.json()
      if (!result.success) throw new Error(result.message || `分片 ${index + 1} 上传失败`)
      return
    } catch (error) {
      if (signal.aborted || attempt === 2 || /HTTP 4\d\d/.test(error.message)) throw error
      await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt))
    }
  }
}
const startUpload = async file => {
  if (uploading.value) return
  const extension = file.name.split('.').pop().toLowerCase()
  if (!supportedExtensions.value.includes(extension) || !file.size || file.size > MAX_UPLOAD_SIZE) {
    adminError.value = '请选择支持的非空问答文件，最大 256 MB；旧版 DOC 需启用 Tika 服务'
    return
  }
  uploading.value = true
  adminError.value = ''
  uploadState.fileName = file.name
  uploadState.stage = '计算文件校验和'
  uploadState.percent = 0
  const controller = new AbortController()
  uploadController = controller
  try {
    if (!user.value) throw new Error('请先登录')
    const sha256 = await hashFile(file, controller.signal)
    if (controller.signal.aborted) throw new DOMException('上传已暂停', 'AbortError')
    const init = (await adminFetch('/customer-service/md/uploads', {
      fileName: file.name, fileSize: file.size, sha256,
    })).data
    if (init.instant) {
      uploadState.stage = '秒传完成（复用已有资料）'
      uploadState.percent = 100
      selectedUploadFile = null
      await loadFiles(1)
      return
    }
    const done = new Set(init.uploadedChunks)
    uploadState.stage = '分片上传中'
    uploadState.percent = 10 + Math.round(85 * done.size / init.totalChunks)
    const missing = Array.from({ length: init.totalChunks }, (_, index) => index).filter(index => !done.has(index))
    let cursor = 0
    const worker = async () => {
      while (cursor < missing.length && !controller.signal.aborted) {
        const index = missing[cursor++]
        await uploadPart(init.uploadId, index, init.chunkSize, file, controller.signal)
        done.add(index)
        uploadState.percent = 10 + Math.round(85 * done.size / init.totalChunks)
      }
    }
    try {
      await Promise.all(Array.from({ length: Math.min(3, missing.length) }, worker))
    } catch (error) {
      controller.abort()
      throw error
    }
    if (controller.signal.aborted) throw new DOMException('上传已暂停', 'AbortError')
    uploadState.stage = '服务端合并与校验中'
    await adminFetch(`/customer-service/md/uploads/${init.uploadId}/complete`, {})
    uploadState.stage = '上传成功，请在列表查看处理状态'
    uploadState.percent = 100
    selectedUploadFile = null
    await loadFiles(1)
  } catch (error) {
    uploadState.stage = error.name === 'AbortError' ? '已暂停' : '上传失败'
    if (error.name !== 'AbortError') adminError.value = error.message
  } finally {
    uploading.value = false
    if (uploadController === controller) uploadController = null
  }
}
const editRemark = async file => {
  const value = window.prompt('编辑备注', file.remark || '')
  if (value === null) return
  try { await adminFetch('/customer-service/md/update', { id: file.id, remark: value }); await loadFiles(pageNo.value) }
  catch (error) { adminError.value = error.message }
}
const retryFile = async file => {
  try { await adminFetch('/customer-service/md/retry', { id: file.id }); await loadFiles(pageNo.value) }
  catch (error) { adminError.value = error.message }
}
const reindexFile = async file => {
  if (!window.confirm('按当前切分规则重新处理“' + file.originalFileName + '”？处理期间该资料将暂时退出检索。')) return
  try { await adminFetch('/customer-service/md/reindex', { id: file.id }); await loadFiles(pageNo.value) }
  catch (error) { adminError.value = error.message }
}
const deleteFile = async file => {
  if (!window.confirm('永久删除“' + file.originalFileName + '”及其向量片段？此操作不可撤销。')) return
  try { await adminFetch('/customer-service/md/delete', { id: file.id }); await loadFiles(pageNo.value) }
  catch (error) { adminError.value = error.message }
}
const loadAudit = async () => {
  const identity = user.value?.id
  try {
    const events = await adminGet('/customer-service/md/audit')
    if (user.value?.id === identity) audits.value = events
  }
  catch (error) { adminError.value = error.message }
}
onMounted(async () => {
  checkReady()
  await restoreSession()
  pollTimer = window.setInterval(() => {
    if (tab.value === 'ops' && user.value && canAdmin.value) loadOperations()
    if (tab.value === 'admin' && user.value && canManage.value && files.value.some(f => f.status < 2)) loadFiles(pageNo.value)
  }, 5000)
})
onBeforeUnmount(() => { chatController?.abort(); uploadController?.abort(); window.clearInterval(pollTimer); window.clearTimeout(agentFileRefreshTimer); filesRequestGeneration++; stopQualityPolling() })
</script>

<style scoped>
.answer-details > summary {
  font-size: 12px;
  color: #637788;
  cursor: pointer;
}
</style>
