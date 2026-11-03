# 医疗资料知识库问答与管理系统

本包用于求职技术评审，包含独立的Python后端、Vue前端、数据库结构、任务Worker、自动化测试和部署模板。重点展示资料可靠入库、混合检索、受限工具调用式预检Agent，以及反馈驱动的知识维护闭环。

没有附带个人运行环境、真实API密钥、管理员密码、数据库数据、备份、会话或客户资料。解压后需要自行配置模型服务和初始化空数据库，不会直接恢复作者的演示账号和历史记录。

## 项目能力

- FastAPI接口与Vue 3页面，SSE增量回答及Markdown展示。
- Markdown、文本型PDF、DOCX、TXT入库，旧版DOC需Tika；结构化分块、Embedding与pgvector索引。
- SHA-256校验、4 MiB分片、断点续传与限定管理范围的内容复用。
- PostgreSQL持久化索引与质量评测队列，独立Worker、租约心跳、重试和提交令牌校验。
- 向量与关键词双路召回、RRF融合和规则筛选。关键词通道不是BM25；PDF重排不是额外训练的神经Reranker。
- 查询者、资料管理员、系统管理员角色，文件授权、版本送审、质量门禁与审核发布。
- 资料预检Agent通过工具白名单执行检查，后端校验权限和前置条件，不提供任意SQL或批准发布工具。
- 用户回答反馈、原始证据快照、人工核实分类、回归题与有效评测报告关联、处理审计。

项目用于公开资料查询和软件验证，不是诊疗系统、医学正确性认证或已验证的生产高可用方案。

## 技术栈

Python、FastAPI、SQLAlchemy、PostgreSQL/pgvector、pg_trgm、OpenAI兼容聊天与Embedding接口、Vue 3、Vite、SSE、Docker Compose。队列由PostgreSQL实现，不依赖Celery或Redis。

## 源码阅读顺序

| 范围 | 入口 |
| --- | --- |
| 接口和流式问答 | backend/app/main.py |
| 上传和续传 | backend/app/resumable_upload.py |
| 文档解析与分块 | backend/app/documents.py、splitter.py |
| 向量入库与混合检索 | backend/app/knowledge.py、pdf_rerank.py |
| 任务可靠性 | backend/app/task_queue.py、worker.py、quality_queue.py |
| 预检Agent | backend/app/precheck_agent.py、precheck_worker.py |
| 权限与版本 | backend/app/auth.py、access.py、versioning.py |
| 评测与反馈 | backend/app/release_quality.py、feedback.py、chat_history.py |
| 页面 | frontend/src/App.vue、PrecheckPanel.vue、FeedbackPanel.vue |
| 数据库 | backend/schema.sql |

进一步说明见[架构](docs/ARCHITECTURE.md)、[功能演示](docs/REVIEW_GUIDE.md)、[测试证据与边界](docs/TEST_RESULTS.md)、[项目说明](docs/PROJECT_NOTE.md)。

## 推荐启动方式

使用Docker运行，避免Windows与Linux依赖锁定文件差异。首次构建需要访问软件仓库并下载镜像。Docker项目名为medknowledge-recruitment，网页端口8090，镜像标签为recruitment-20261027；与作者原演示的项目名、镜像标签和8088端口分开，但仍应先检查本机是否冲突。

在解压后的本目录打开PowerShell：

```powershell
Copy-Item deploy/staging.env.example deploy/staging.env
python -c "import secrets; print(secrets.token_hex(24))"
```

编辑deploy/staging.env，将随机值填入POSTGRES_PASSWORD，并填写LLM_BASE_URL、LLM_API_KEY、CUSTOMER_MODEL和EMBEDDING_MODEL。聊天和Embedding模型名称、接入地域与1536维输出必须由自己的服务支持。配置错误不能靠示例占位符启动；可选本机兼容模型服务，但本包没有内置大模型。

保持PROJECT_DB_NAME=medknowledge_stage。该数据库位于本包独立Compose项目的db容器中，不要改为已有业务数据库或挂载原业务数据卷。默认LOCAL_DEMO_ALLOW_SELF_APPROVAL=false，采用资料管理员上传、系统管理员独立审批，不自动开启单管理员例外。

```powershell
$dc = @('--env-file', 'deploy/staging.env', '-f', 'compose.staging.yml')
docker compose @dc config --quiet
docker compose @dc build api web
docker compose @dc up -d db tika
docker compose @dc --profile maintenance run --rm migrate
docker compose @dc up -d api index-worker quality-worker precheck-worker ops web
docker compose @dc ps
```

首次管理员密码仅在本机Docker持久卷中生成，可自行查看，勿转发该输出：

```powershell
docker compose @dc exec api python -c "from pathlib import Path; print(Path('/app/.credentials/admin-bootstrap.json').read_text())"
```

打开http://127.0.0.1:8090，使用admin及生成密码登录，及时修改密码。初始数据库没有资料，按照[功能演示](docs/REVIEW_GUIDE.md)创建editor和viewer，再上传本包的非医疗样例。

`/api/ready`只检查应用就绪条件，不保证模型供应商响应。可执行以下命令验证聊天/Embedding配置，会消耗少量模型额度：

```powershell
docker compose @dc exec api python -m scripts.probe_model_provider
```

停止服务但保留数据：`docker compose @dc stop`。删除Docker卷会清空该演示的数据，不要随意运行带`-v`的清理命令。

## 测试

前端测试：在frontend中运行npm ci，再运行npm test、npm run build。后端测试位于backend/tests，部分测试需要PostgreSQL或公开PDF原件；不应把跳过项当作通过。

若需验证包含数据库的反馈用例，可在此独立环境中执行专用临时库脚本，结束后仅清理其创建的随机测试库：

```powershell
docker compose @dc run --rm --no-deps -v "${PWD}/backend/tests:/app/tests:ro" -v "${PWD}/backend/evaluation:/app/evaluation:ro" -v "${PWD}/backend/data:/app/data:ro" api python -m scripts.verify_feedback --all
```

正式模型评测与故障注入脚本会消耗模型额度或影响Worker，不作为普通健康检查默认执行。若使用pip安装，Windows优先参考requirements.txt；requirements.lock是Linux容器的固定依赖快照。

## 演示资料与运维脚本

examples/提供非医疗合成样例；backend/data保留少量公开摘录及解析测试夹具。完整器械PDF未打包，public_manuals目录仅包含来源清单，相关测试会跳过或需自行按来源准备。

backend/scripts中保留了完整开发和验收脚本，部分针对作者历史数据库的固定ID、状态或手工资料核对，不能在新库直接照抄运行。优先使用通用初始化、探测、隔离测试及页面操作；特别不要把特定PDF技术核对脚本当成通用医学审批工具。

这是源码评审包，没有附带数据迁移快照或客户端业务资料。依赖、第三方来源和非公开内容排除说明见[第三方与打包说明](docs/PACKAGE_NOTES.md)。
