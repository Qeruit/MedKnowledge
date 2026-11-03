# 打包与第三方说明

## 保留内容

后端源码、前端源码、任务和评测工具、数据库schema、自动化测试、Docker构建文件、配置模板、少量公开/合成测试夹具、历史测试摘要及启动说明。代码和依赖已有的作者、来源、版权及许可证信息保持原样，本包不新增对第三方内容的授权声明。

## 排除内容

真实.env/.env.local/deploy环境文件，管理员初始化凭据、会话令牌、数据库备份/恢复数据、上传存储、运行日志、Git历史、node_modules、虚拟环境、缓存、构建产物、个人简历/身份证/面试材料、截图和完整第三方说明书PDF。

配置副本仅为隔离求职演示作调整：Docker项目名medknowledge-recruitment，默认HTTP端口8090，镜像标签recruitment-20261027，自审批默认关闭；原项目代码和运行配置未改动。

## 第三方资料

- backend/assets/tiktoken包含公开cl100k_base词表，来源和SHA-256见该目录README，Docker构建时核验。
- backend/data/apache_tika_testWORD.doc为解析测试夹具，保留相邻APACHE_TIKA_LICENSE、NOTICE及FIXTURE_NOTICE。
- backend/data/actastim_s_verified_demo.md是带来源的部分公开说明书摘录，仅作为资料查询与软件评测样例，不是完整/官方医学知识库。
- public_manuals目录仅附公开来源清单和元数据，未附整份手册。按清单下载时应自行核对条款、来源与哈希，资料更新不应自动代替审核。
- Python与JavaScript第三方依赖遵循各自许可证。本包不附原私有依赖目录，安装和镜像构建需网络。

MANIFEST.sha256记录包内文件哈希，用于校验传输完整性，不是数字签名、安全认证或医学认证。
