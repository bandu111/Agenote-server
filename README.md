# Agenote Server

Agenote 的 FastAPI 后端，将真实会议证据连接到纪要、知识库、行动项、持续目标、双方承诺和职业工作成果。配套客户端：[Agenote-app](https://github.com/bandu111/Agenote-app)。

此仓库同步原 `yanshu_server/main` 的业务代码，保留现有模块名、数据库结构和 API 路径；仅整理仓库内容，不复制生产数据和密钥。

## 核心能力

| 模块 | 提供什么 |
| --- | --- |
| 账户与职业画像 | 注册、登录、会话轮换、资料与个性化保存、职业归类和分析偏好 |
| 实时会议 | 腾讯云 ASR V2 代理、稳定转写同步、术语、重点、风险与打断补全相关分析 |
| 会议归档 | 结构化纪要、后续行动、思维导图数据、任务进度事件、原话依据 |
| 音频资料 | 私有整场录音存储与授权片段引用、按账号访问原声 |
| 目标与承诺 | 目标确认、跨会议明确续接、里程碑、承诺补全与确认、履约投影 |
| 知识库 | 文本和附件解析、文件夹、检索、版本与引用、资料删除和纪要关联 |
| 微信 ClawBot | 逐账号扫码绑定、全局会议问答、知识库问答、疑问追问、摘要及 PDF 发送 |
| 职业工作成果 | 会后结合职业和会议证据规划一份报告，引用真实资料，标明数据缺口 |
| 会前准备 | 对有效未来日程生成准备建议，并在允许的时间窗口尝试微信提醒 |
| 声纹 | 单独同意、分段注册、独立验证、模板加密及会议本人识别 |

模型输出先经过来源、版本和对象权限校验，才影响正式业务状态。相同标题不自动证明是同一个目标；会议口头汇报与履约核验分别记录。

## 关键业务规则

- 少于 **1 分钟**的会议不新建 AI 纪要。
- 满 **3 分钟**且已结束、完成可用纪要归档、用户已启用自动分析的会议，进入自动职业报告流程；不再仅因规划模型返回“没有额外问题”就跳过。保持每场一次自动报告的去重、调用配额、引用与数值校验。
- PDF 生成和微信发送仍可能失败；绑定、会话上下文、推送偏好、配额和真实发送结果分别记录，不伪造送达。
- 微信全局问答不要求先打开某场会议，只检索绑定账号有权访问的内容。
- 会前准备默认配置关闭；开启后仅处理经过确认和回读核验的有效日程。通常目标窗口为开始前 **60 分钟**；近时确认需满足最少 **15 分钟**等策略，取消、改期和过期会使旧准备失效。
- 目标有既定里程碑时，后续会议只更新原有节点，避免将“已经完成”等进度句追加成新节点。会议汇报不自动代表独立验收通过。
- 后台报告和疑问任务不阻塞纪要接口。业务提醒由客户端本地通知展示，微信消息由独立的绑定通道发送。

## 环境要求

- Python **3.12–3.13**，PostgreSQL **17**。
- Docker Compose 可用于启动本地数据库；测试必须使用独立的 `yanshu_auth_test` 库。
- Hermes 为单独安装的官方依赖，版本固定在 `requirements-hermes.txt`；按部署设置 `HERMES_RUNTIME_PATH`。
- OCR/Office 文档解析按需要安装 Tesseract、Poppler、LibreOffice；QMD 适配器为可选组件。
- 声纹启用时需要额外的模型文件、CPU PyTorch/SpeechBrain 依赖及独立加密密钥。

## 本地启动

```bash
git clone https://github.com/bandu111/Agenote-server.git
cd Agenote-server
python -m venv .venv
```

激活虚拟环境（Git Bash）：

```bash
source .venv/Scripts/activate
```

macOS/Linux 使用 `source .venv/bin/activate`。随后：

```bash
python -m pip install -r requirements-dev.txt
python scripts/bootstrap_local.py
docker compose up -d db
python -m alembic upgrade head
python scripts/setup_action_checkpoints.py
python scripts/serve.py
```

`bootstrap_local.py` 只在 `.env` 不存在时创建独立本地配置，不覆盖已有配置，也不输出生成的密码。基础启动后访问：

```text
GET http://127.0.0.1:8001/health
API 前缀：http://127.0.0.1:8001/api/v1/
```

基础 API 可启动不代表所有 AI 能力已就绪。模型、腾讯 ASR、Hermes、微信绑定及声纹应按实际需要分别配置。

## 配置

所有敏感值只保存在部署环境或 `.env`。仓库只提供 `.env.example`。

| 配置组 | 常用变量 |
| --- | --- |
| 数据库与账户 | `DATABASE_URL`、`TEST_DATABASE_URL`、`JWT_SECRET` |
| 腾讯 ASR | `TENCENT_ASR_APP_ID`、`TENCENT_ASR_SECRET_ID`、`TENCENT_ASR_SECRET_KEY`、`TENCENT_ASR_ENGINE_MODEL_TYPE` |
| 轻量模型 | `DASHSCOPE_API_KEY`、`QWEN_BASE_URL`、`QWEN_MODEL`、`QWEN_WORKERS_ENABLED` |
| 主模型 | `DEEPSEEK_API_KEY`、`MINIMAX_API_KEY`、`MINIMAX_MODEL`、`MINIMAX_WORKERS_ENABLED` |
| Hermes | `HERMES_RUNTIME_PATH`、`HERMES_TIMEOUT_SECONDS` |
| 微信账号绑定 | `WECHAT_BINDING_KEY`，用于加密每个用户独立的微信连接 |
| 会前准备 | `MEETING_PREPARATION_ENABLED=true`、`MEETING_PREPARATION_RECOVERY_MINUTES=15` |
| 声纹 | `VOICEPRINT_KEY`、`VOICEPRINT_KEY_ID`、`VOICEPRINT_MODEL_PATH` 及 `VOICEPRINT_LIVE_ENABLED` |
| 可选 QMD | `QMD_URL`、`QMD_TIMEOUT_SECONDS` |

生产 `.env`、systemd 覆盖配置和用户微信授权不随 Git 仓库迁移。更换仓库不会自动开通通道、复制密钥或启用会前准备。

安装 Hermes：

```bash
python -m pip install -r requirements-hermes.txt
```

声纹按需安装：先安装与平台匹配的 PyTorch/torchaudio CPU 版本，再安装 `requirements-voiceprint.txt`，准备匹配版本模型，并使用 `python scripts/setup_voiceprint_local.py` 生成独立本机密钥。已有模板时不可随意更换密钥。

## API 概览

以下路径均以 `/api/v1` 为前缀；对象读取和变更都应携带当前账户凭据。

| 路径 | 用途 |
| --- | --- |
| `/auth/*`、`/me/*` | 账户认证、会话和资料 |
| `/meetings/*` | 会议同步、分析作业、归档、音频与身份信息 |
| `/meetings/{meeting_id}/questions/*` | 疑问记录、分析、修改与建议 |
| `/home` | 首页任务、日程、节奏与通知数据 |
| `/progress/*` | 目标、承诺和里程碑 |
| `/knowledge/*`、`/search/*` | 资料管理、解析、检索与引用 |
| `/actions/*` | 日历批准、设备回执、取消和改期 |
| `/assistant/wechat*` | 独立微信扫码绑定、解绑及推送偏好 |
| `/assistant/queries*` | 全局会议/知识库问答任务及查询历史 |
| `/assistant/outcomes*` | 成果列表、详情和 PDF |
| `/me/voiceprint/*` | 声纹注册、验证和管理 |

具体字段以当前 router/schema 为准；生产可能关闭在线 OpenAPI，不应以 `/docs` 是否开放判断服务是否正常。

## 目录结构

```text
app/core/                 配置、数据库、鉴权基础、日志和应用生命周期
app/integrations/hermes/  模型适配、执行能力与运行所需技能资源
app/modules/              按业务模块划分的 router / service / schema / model
migrations/               Alembic 数据库版本
tests/                    账户隔离、业务规则和故障恢复测试
scripts/                  本地初始化及启动所需脚本
tools/qmd/                可选 QMD 检索适配器
assets/fonts/             PDF 中文字体及许可
compose.yaml              本地 PostgreSQL
requirements*.txt         运行、测试与可选依赖
```

`app/integrations/hermes/skills/` 中的 Markdown、许可、脚本和来源信息是运行资源，不能当作个人文档删除。

## 测试

先准备独立测试数据库，禁止将测试连接指向真实用户库：

```bash
python -m pytest tests/test_home_milestone_continuity.py tests/test_milestone_meeting_reports.py tests/test_progress_relations.py tests/test_progress.py -q
python -m pytest tests/test_assistant_queries.py tests/test_meeting_preparation.py tests/test_wechat_binding.py -q
python -m pytest tests -q
```

测试夹具会清理 `yanshu_auth_test` 的账户与检查点。不要让多个测试进程共用同一测试库。模拟微信传输和模型测试只证明逻辑，不证明实际微信到达或真实模型性能。

发布前应分别验证真实 ASR、会议归档、账号隔离、微信问答、一次 PDF 发送、会前窗口、取消/改期和失败重试。项目仍有需单独核对的既有全量测试失败，不能把局部测试通过等同于全量验收通过。

## 部署

部署前备份数据库和当前代码，安装固定依赖，执行 `alembic upgrade head`，由 systemd 或容器托管服务。健康探测使用 `/health`；模型和微信能力还需要单独检查后台任务与通道状态。

数据库、上传录音、声纹模型、PDF 输出及加密密钥应使用独立持久化目录。迁移涉及表结构时，代码回滚不等于数据库自动回滚，必须按迁移兼容性处理。

仓库不包含生产 `.env`、SSH 凭据、数据库备份、用户文件、测试输出和旧的一次性部署脚本。此仓库提供服务源码及启动方式，不默认附带云端部署工作流。
