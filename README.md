# 拾忆 Shiyi Memory · 工作记忆助手与桌面宠物

一个运行在 Mac 上的工作记忆助手：Swift 通过 AX 和本机 PaddleOCR 采集当前窗口中的文本，FastAPI 将业务记录和持久任务存入 PostgreSQL，摘要与问答默认使用本机 Ollama，也可在网页配置局域网／云端生成服务；嵌入保持使用本机 Ollama。React 网页与 Swift 原生桌宠共用完整 Pi Coding Agent SDK，支持绑定项目后的自主编程与上下文压缩，并提供证据问答、需要确认的应用操作、项目恢复、日报/周报、待办与提醒。

业务数据库统一为 **PostgreSQL 17**，没有业务 SQLite、Redis 或 pgvector。默认记忆后端为 **ReMe**，可以切换为 **Graphiti + Neo4j 5.26**。框架的记忆内容与索引通过公开 API 操作。

Web 与桌宠的使用、记忆替换、历史回填和皮肤协议见 [个人版实施说明](docs/personal-companion.md)。四个板块的职责、对象操作与联网设置见 [模块架构](docs/mcp-architecture.md)。采集、记忆检索与 Pi 的实际连接见 [当前数据流图](docs/data-flow.md)。

单击桌宠打开聊天，再点一次关闭；关闭后 Agent 继续回答，应用未退出时保留未发送草稿。可在网页“偏好与状态 → 桌宠形象”上传 PNG，或右键桌宠选择“导入 PNG 形象…”。形象在本机保存、两端共享，换图无需重新编译或授权。

## 开始使用

项目仓库：[xxddccaa/shiyi-memory](https://github.com/xxddccaa/shiyi-memory)。先将源码下载到本机：

```bash
git clone https://github.com/xxddccaa/shiyi-memory.git
cd shiyi-memory
```

首次使用：双击项目中的 **拾忆管理.command**，选择“安装／修复”。首版支持 Apple Silicon Mac、macOS 14 及以上；缺少的开发工具、Python、Node、Homebrew PostgreSQL 17 和模型会被检查并引导补齐。系统授权和首次安装 Homebrew 需要本人完成，其余步骤自动执行，中断后可以继续。

以后在同一菜单选择启动、停止、重启、查看状态、诊断、日志、备份或卸载。默认卸载保留记忆、配置、模型和备份。详细说明见 [普通用户管理指南](docs/management.md)。

```bash
bash scripts/manage.sh install        # 安装／修复，并启动
bash scripts/manage.sh start          # 使用已有安装启动，不重新签名
bash scripts/manage.sh status         # 中文状态
bash scripts/manage.sh status --json  # 程序可读状态
bash scripts/manage.sh stop
```

网页默认端口 **8765**。启动时自动打开网页，访问令牌通过 URL fragment 传递，不写入服务访问日志。模型未就绪时文字仍可入库，摘要任务会退避重试。所有应用路径从项目目录推导，可以从其他工作目录调用脚本。

当前依赖的具体版本见 `uv.lock`、`frontend/package-lock.json`，可选容器镜像的 digest 见 `infra/images.lock.json`。模型下载量以实际 manifest 和 inventory 为准。

本机 Docker Hub 连接异常，Neo4j 镜像采用 Google 的 `mirror.gcr.io/library/neo4j` 镜像源；已核对其 manifest digest 与上游相同，并在锁文件记录上游 digest。未修改系统 DNS 或 Docker 全局配置。

## Mac 采集

```bash
./scripts/collector-start.sh
```

首次需要在叶子菜单授予辅助功能和屏幕录制权限，点击“开始采集”并同意。默认定时采集无需输入监控权限。以后恢复上次选择，用户暂停会保持暂停。更新采集端二进制后 macOS 可能要求重新授权，普通启动不会重新构建或签名。

- 默认以 **PaddleOCR 3.7.0 + PP-OCRv6 tiny** 读取窗口正文，AX 辅助定位窗口和敏感输入。
- 默认 **每 5 秒单张截图并运行 PaddleOCR**，不持续录屏，也不监听点击或按键。OCR 串行，上一轮未完成就跳过本次采样，不积压、不补采；锁屏或睡眠时挂起。
- 截图只在内存和本机 OCR 服务之间传递，不写入磁盘。`capture_mode: interaction` 仍可手动启用操作前后采集，此模式才需要输入监控权限。
- 文本相似度达到 95% 时减少模型输入，但保留日期、金额、否定、状态与错误等重要变化。同片段近似重复保存可逆差异；定时采集跨五分钟片段也会去重，静止页面整晚不产生新片段或新的大模型任务。基准保存在 PostgreSQL，进程重启后仍有效，直到原文保留期届满。
- OCR 完成的文本原子写入 `data/collector/spool/`，确认入库或过滤后才删除。默认队列上限 50 MB，满时暂停，不覆盖旧批次；上传与采集分别调度。
- 五分钟活动片段共用一个可合并摘要任务，高频采集不会按点击数量调用大模型。暂停采集时，已有文字仍会尝试上传。

网页首页常驻采集状态，展开可分别查看最近截图、OCR、收到上传及最新入库记录的采集时间；权限未生效时提供当前应用的修复步骤。菜单栏也提供采集状态。重复页面正常过滤，不能仅凭没有新入库判断采集故障。OCR 服务就绪不能证明自动采集已开启；OCR 故障不会自动改用 AX。

采集范围由 `config/app.local.yaml` 中 `collector.excluded_apps`、`collector.excluded_title_patterns` 控制。修改后运行 `uv run python scripts/local_config.py` 并重启采集端。排除规则不能自动识别所有敏感内容；可随时暂停或删除片段。

PaddleOCR 的安装、验证与权限说明见 [docs/ocr.md](docs/ocr.md)。

## 配置与本地服务

在网页 **偏好与状态 → 整理与问答模型** 自主选择本地 Ollama 或 OpenAI 兼容的局域网／云端服务，填写模型名、基础地址及可选密钥，测试后保存即可生效。嵌入与 OCR 保持本机运行。操作与故障排查见 [网页模型配置指南](docs/models.md)。

| 内容 | 路径 / 默认值 |
|---|---|
| 可提交的配置模板 | `config/app.example.yaml` |
| 本地应用配置 | `config/app.local.yaml` |
| 网页保存的生成模型配置 | `config/generation.local.json`，包含可选密钥，权限 0600 |
| MCP 服务配置 | `config/mcp.local.json`，含可选凭据/环境变量，权限 0600 |
| 本地凭据 | `config/secrets.env`，权限 0600 |
| 网页/API | 本机端口 8765 |
| PostgreSQL | 本机端口 5432，数据库与用户 `workmemory` |
| 原生 Ollama | 本机端口 11434，程序在 `.cache/ollama/` |
| 模型文件 | `models/ollama/` |
| PaddleOCR | 本机端口 8766；独立 Python 环境 `ocr/.venv/`，锁定依赖 `ocr/uv.lock` |
| OCR 模型与摘要清单 | `models/paddleocr/`、`models/paddleocr/inventory.json` |
| PostgreSQL 数据 | `data/postgres-native/`；旧 `data/postgres/` 仅作迁移前保留 |
| 数据库归属与迁移记录 | `config/database.local.json`（不提交） |
| ReMe 工作区 | `data/reme/` |
| Neo4j 文件 | `data/neo4j/{data,logs,import,plugins}/` |
| 日志 / 进程记录 | `logs/`、`.run/` |

原生应用更新后需要重新授权时，可双击项目根目录的 `修复拾忆权限.command`。脚本清理本应用旧授权并引导系统点击，完成后自动核验；不需要 sudo，也不会再次构建或签名。详见 [权限修复](docs/management.md#采集权限)。

原生 PostgreSQL 17 通过 Homebrew 安装，项目使用 `pg_ctl` 启停独立数据目录，不注册全局 `brew services`。只监听 `127.0.0.1`；共享缓冲 128 MB、每个排序/哈希操作的 work_mem 4 MB、32 个连接，保持事务持久性设置。应用连接池为 4，最多临时增加 2 个连接。默认 ReMe 无需 Docker；可选 Neo4j 使用项目 bind mount，容器上限 2 GB、堆 512 MB、页缓存 256 MB。

如果端口被占用，脚本会报错。修改端口前先停止本项目服务，再编辑本地配置；Neo4j 的 Bolt 端口在 `memory.neo4j_uri`，HTTP 端口在 `memory.neo4j_http_port`。不会接管已有 Ollama 服务。

```bash
./scripts/doctor.sh
./scripts/dev-down.sh
```

`dev-down.sh` 先退出采集端，再停止本项目 API、Ollama、PaddleOCR、原生 PostgreSQL 和本项目可选 Neo4j 容器，数据仍保留。

本机 npm 若配置了不可达的代理，`scripts/npm.sh` 只对本项目的命令使用临时空配置和直连，不修改用户或全局 npm 配置。

## 聊天操作与联网工具

没有明确项目的记录默认显示为“日常工作”。时间线与原文搜索可单独筛选，项目空间提供默认分组入口，回顾与确认记忆使用同一名称。默认分组沿用未指定项目的记录，不新增占位项目，也不让模型猜项目名；手动添加记录时，项目名称留空即可。已有明确项目继续按原归属整理，项目删除后保留的记录回到日常工作。

Web 与桌宠均可“选择对象”后让助手处理指定待办、项目、记忆或片段。待办页也提供“交给助手处理”；删除需要核对卡片并确认；“把我的待办全部删除”会生成一张完整批量清单卡片，无需逐条提案。删除会清理待办正文及依赖内容，并阻止旧摘要后台抽取把它重新创建。原工作记录有独立生命周期。

项目空间提供“预览删除全部项目”，聊天也可直接说“删除我的所有项目”：一张完整清单卡片、一次确认。删除项目分类及别名，原始记录、待办提醒和确认记忆保留为未归属；不删除本机项目文件。网页标签页、登录页与侧栏共用叶子图标。

“偏好与状态 → 联网与工具”预置 Exa 搜索、Open-Meteo 天气，默认关闭；用户启用后两端共享。可配置受信任的只读 HTTP/stdio MCP 服务，业务写操作仍须用户确认；本机编程由聊天中的目录开关单独启用。“今日份工作”可按本地日期浏览当日来源并读取有效日报；有原文但未整理时会提供状态和有限原文证据。

## 桌宠主动陪伴

桌宠可以偶尔主动冒出一句简短俏皮话：闲聊、含蓄工作鼓励、天气关心或邀请休息。到“偏好与状态 → 桌宠与工作整理 → 主动陪伴”调整频率、话题、安静时段，并用提示词定制专属性格。默认10–20分钟一次机会、每天最多30次；气泡10秒消失，点击才接入聊天，不自动发送消息。每句话由当前模型经共享 Pi 生成，忙时跳过，无固定话术兜底。全屏、锁屏、聊天、正式提醒或节能时安静，恢复后不补弹。天气须先启用工具并设置常用城市；不会因此开启采集。

## 主动设置定时事项

采集和工作摘要不再自动提取待办。网页“待办与提醒”填写事项、具体日期时间、时区和重复规则后保存；也可以对 Pi 说“明天下午三点提醒我交周报”，核对卡片再确认。没有给出时间时先询问，不猜测时间。支持一次性、每天、周一至周五和每周指定星期。

到点由独立调度器写入共享会话，小猫显示安静、无焦点的气泡，可以打开对话、完成本次或延后十分钟。重复事项的“完成本次”保留以后提醒；暂停/取消作用于整个日程。睡眠或应用关闭期间不会唤醒电脑，恢复后合并补报。详见 [定时提醒说明](docs/personal-companion.md#定时事项与小猫提醒)。

## 本机编程

在 Web 或桌宠聊天中展开“本机编程”，添加已有项目目录，点击“新建编程会话”，即可让拾忆读代码、修改文件、执行测试命令。工具执行记录展示文件差异和完整命令输出；两端使用同一会话，可以继续或停止同一个任务。

开启后自主执行，不逐条批准。代码以当前本机用户权限运行，工作目录不是沙箱。自动读取项目 `AGENTS.md`、`.pi/skills`、`.agents/skills`、`.pi/prompts`，本地代码扩展需要显式添加。应用待办和记忆写操作仍走确认卡片。模型容量须在模型设置填写，文件及命令内容会交给当前保存的模型服务。移除目录登记和删除会话不会删除项目文件。

## 记忆切换、纠正和删除

首次准备 Graphiti：

```bash
./scripts/dev-up.sh --graphiti
```

然后在网页“偏好与状态”选择 Graphiti。目标后端初始化成功后才更新选择，并从仍保留的摘要回放历史。同步进度和失败任务会显示在网页中。选择保存在 PostgreSQL，重启后保留；后续 `dev-up.sh` 会按已保存的选择启动需要的 Neo4j 服务。

实现中的一致性规则：

1. 采集业务记录与摘要任务在同一个 PostgreSQL 事务中提交。
2. 任务通过 `FOR UPDATE SKIP LOCKED` 领取，持有 90 秒租约，每 25 秒续期；旧租约令牌不能确认新任务。失败最多重试 8 次，退避最多 300 秒。
3. 片段按设备、项目提示和 5 分钟窗口划分。仅当前版本可以写回摘要。原始采集窗口闭合后整理；手动记录立即排队。
4. 普通记忆写入只发给当前选定后端。纠正和删除登记给所有曾使用过的后端。
5. 外部写入期间不持有业务行锁。写入完成后核验版本，落后的写入不记为同步成功，并安排修复。
6. 检索后端只提供候选来源 ID。回答、时间线和来源详情始终使用 PostgreSQL 中未删除、当前有效的记录。模型回答期间发生纠正或删除，整个回答会失效。
7. 删除清理文本、摘要、关联待办和提醒，并保留不含正文的事件 ID、版本和删除标记，阻止离线重传或切回旧后端时恢复内容。
8. 纠正会替换摘要并清除旧原文和依赖旧内容的待办/提醒。保留明确的“已纠正”标识，避免把旧原文误当成纠正后的证据。

ReMe 使用应用提供的扩展嵌入组件，向同一个 Ollama 调度器发送批量请求；中文关键词和向量检索由 ReMe 完成。写入/删除后显式同步并持久化索引，不依赖文件监听器的时序。

Graphiti 使用其公开的 LLM、嵌入和重排接口。每个业务片段使用独立的 graph group，确保删除一个片段时，共享实体摘要不会残留该片段的事实。跨片段项目关联、时间顺序和事实变化由业务证据层组装；这是首版对删除可靠性的取舍，暂不在 Graphiti 中合并跨片段实体。Neo4j 为本项目专用，检索结果仍需通过业务来源过滤。

“重建记忆索引”从业务摘要重新写入当前后端，保留版本核验和删除标记，不直接修改框架内部文件或图结构。

## 保守原文记忆

自动整理只让模型选择原文块，由后端保存完整原句、时间、应用及可定位的来源。它不能自由生成“你完成了…”之类的个人事实；原句里的第三方、否定、条件与草稿上下文一起保留。无法挑出合适内容时显示“无可提炼内容”，正常结束任务。

时间线和来源详情区分屏幕摘录、用户确认、等待提炼和原文到期。旧自动摘要先保存到“查看隔离历史（仅供本人核对）”，不交给模型；开启“偏好与状态 → 历史原文复核”后分批核对尚存原文。原文已缺失的旧记录仍可由本人查看，但不会重建成新记忆。

日报、周报和项目简报的自动部分只列原句“屏幕摘录”，不再逐层改写成个人成果。手工确认的内容仍可整理，人工回顾稿与工作段编辑保留。检索、Pi、报告和主动陪伴统一核验来源，删除、纠正或到期也清理依赖内容。

## 保留期、回顾与提醒

默认原始文本保留 30 天，包括尚未整理的积压。超过保留期但未生成摘要的片段标记为已过期；超过保留期才上传的原始文本会被确认过滤。未经用户确认的自动摘录也随原文到期；用户明确确认的独立记忆及纠正长期保留，除非主动删除。

每日回顾按配置时区列出当日已整理片段，18 点后出现回顾提醒。项目可以主动生成“恢复上下文”提示。待办只由用户主动设置，明确日期、时间与时区并确认后才会提醒，采集和整理不自动创建。

提醒仅显示在工作记忆助手中，不控制其他应用，也不发送外部消息。

## Agent 执行额度

默认每轮最多 **1,024 次模型请求、16,384 次工具调用、4,096 次记忆检索、4,096 次外部查询**；累计工具资料 1,600 万字符，整轮最长 24 小时。任务完成即结束，也可随时取消。这些是防循环执行上限，与模型是否收费无关。

`config/app.example.yaml` 的 `agent` 节列出配置；已有本地 YAML 缺少该节会自动采用新默认值，显式覆盖优先。修改本地配置后重启后端生效。网页高级参数可分别填写模型上下文容量和单次最大输出，聊天直接使用保存的输出值。模型上下文由完整 Pi Coding Agent 的自动摘要压缩管理，也可点击“立即压缩”；原生会话条目、摘要边界、执行日志与来源依赖都保存到 PostgreSQL。详见 [Pi 会话与压缩](docs/embedded-agent.md#pi-会话与上下文压缩)。

## 模型调度与性能

一个原生 Ollama 服务使用以下初始设置：

```text
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_CONTEXT_LENGTH=8192
OLLAMA_NO_CLOUD=1
```

生成默认使用 `qwen3:8b`，关闭 thinking，网页可独立切换生成服务；嵌入使用 `qwen3-embedding:0.6b`，1024 维，每批最多 8 条。交互问答在待执行队列中优先于后台任务；已运行的模型请求不会被强行中断。Graphiti 内部并发为 1。

API 以一个进程运行，本地进程文件锁和 PostgreSQL 会话锁防止第二个应用进程同时管理同一 ReMe 工作区；数据库重启时本地进程锁仍然有效。模型调用记录生成耗时、等待时间、加载/切换耗时，网页“偏好与状态”可查看。

```bash
uv run python scripts/benchmark.py --iterations 3
```

基准使用 `tests/fixtures/chinese_work.json` 的合成中文记录，通过实际 API、持久任务和当前记忆后端测试，核验事实变化与证据来源，记录端到端每片段耗时是否低于 300 秒。完成后删除测试片段。报告保存到 `data/exports/benchmark-*.json`。没有实际模型运行记录时，不宣称吞吐或内存压力已经达标。

## 日常对话回归

```bash
uv run python scripts/evaluate_dialogues.py --suite smoke
uv run python scripts/evaluate_dialogues.py --suite full
```

模拟用户通过真实 Pi 和应用接口完成日常事务，独立 27B 评审与数据库断言共同判定结果。固定库覆盖 66 个任务、198 种说法，支持失败复现、重复运行、HTML 回放和历史对比。使用独立 PostgreSQL/ReMe 与合成资料，不操作正式数据。运行方式、模型选择及评测边界见 [对话回归评测](docs/dialogue-evaluation.md)。

## 备份与恢复检查

```bash
./scripts/backup.sh
./scripts/restore-check.sh data/backups/workmemory-YYYYMMDD-HHMMSS.dump
```

使用 `pg_dump --format=custom`，先写 `.partial`，验证归档后原子改名。恢复检查创建临时数据库、执行 `pg_restore --exit-on-error` 并检查迁移和记录，最后删除临时库，不覆盖业务库。

需要实际恢复时，先停止 API，按选定备份恢复业务 PostgreSQL，再使用网页的重建功能从摘要补齐记忆。原始数据库目录不能作为运行中的备份来源。备份含创建时的历史内容；主动删除不会改写已生成的备份，可按自己的保留要求清理旧备份。

## 验证

```bash
uv run pytest -q
uv run ruff check backend scripts tests
./scripts/npm.sh run build
swift run --package-path macos SpoolCheck
swift run --package-path macos InteractionCheck
WORKMEMORY_BROWSER_TEST=1 uv run pytest -q tests/integration/test_agent_web.py
```

Python 集成测试创建独立、随机命名的 PostgreSQL 测试数据库，并执行真实 Alembic 迁移，结束后删除。它们覆盖事务回滚、并发去重、租约恢复、旧令牌确认、切换/纠正/删除、慢写入与删除竞争、回答期间删除、保留期和提醒确认。ReMe 测试使用真实框架的工作区与索引，模型使用确定性替身；Graphiti 的离线检查验证已安装版本的客户端接口，不能代替真实 Neo4j 和模型验收。

Swift `SpoolCheck` 无需 XCTest 或完整 Xcode，验证离线重启、未确认不删除、容量满不丢弃。浏览器测试使用本机 Chrome，由 Python 测试入口启动隔离 API 和 PostgreSQL 库，创建并删除合成记录，覆盖主要网页流程与窄屏布局。

下列基础设施检查建立独立的原生 PostgreSQL 实例与随机端口，不停止业务服务，也不修改采集选择：

```bash
uv run python scripts/verify_infra.py
```

它验证重复启动复用进程、重启后数据保留、归属检查及含数据备份的完整恢复。Swift 离线文件保留与重试幂等由 SpoolCheck 和采集集成测试另行验证。结果在 `data/exports/infra-verification.json`。

`uv run python scripts/verify_graph_storage.py` 使用真实 Graphiti / Neo4j 和确定性模型替身，验证 1024 维写入检索、Neo4j 容器重建、纠正和删除；会短暂重建本项目 Neo4j 容器。报告在 `data/exports/graph-storage-verification.json`。它验证存储与客户端兼容性，不评估 Qwen 效果。

完整的真实模型及双后端验收：

```bash
./scripts/acceptance-live.sh
```

该脚本准备 Neo4j 和模型，执行真实后端历史回放、纠正传播、删除后切换防复活，以及三轮中文吞吐基准。下载或服务尚未就绪会明确失败，不能把离线替身测试当作真实模型验收。

本次环境的已完成检查与尚未完成项记录在 `docs/verification.md`。

## 开发

```bash
./scripts/frontend-dev.sh       # Vite 5173，代理当前配置的 API 端口
uv run python scripts/serve.py # 直接运行 API；确保未启动另一个应用进程
```

变更数据库模型后，用 Alembic 生成并审阅迁移；已提交的初始迁移是固定 DDL，不依赖运行时模型的 `create_all()`。

```text
backend/workmemory/  API、配置、PostgreSQL、采集处理、片段、记忆适配、推理、问答、提醒、任务
agent/               锁定的 Pi Coding Agent SDK、Node 会话层与桥接协议
backend/workmemory/workspace/  业务对象、统一工具定义与操作事务
backend/workmemory/mcp/        本机 MCP、外部只读工具、配置与来源
frontend/            React + TypeScript + Vite
macos/               Swift 菜单栏、操作监听、内存画面缓存、AX 辅助与文本队列
ocr/                 独立 PaddleOCR 环境、图像识别服务和接口测试
config/              配置模板、ReMe 配置和提示词
infra/               Compose 与镜像 digest
scripts/             初始化、服务管理、下载、备份、检查和基准
```

模型、个人数据、凭据、日志、缓存、编译产物和虚拟环境均由 `.gitignore` 排除。

## 参考项目与开发规划

- [参考仓库与快照清单](references/README.md)：七个上游仓库的本地浅克隆、版本和源码入口。
- [源码对照与改进建议](docs/reference-analysis.md)：当前实现的具体缺口、各家方法及适配取舍。
- [分阶段开发规划](docs/development-plan.md)：数据模型、接口、开发任务、验收指标和迁移顺序。

## 内置助手与工作上下文

网页“问问记忆”和菜单栏“和桌宠聊天”共用应用托管的完整 Pi Coding Agent SDK：持续会话、主动检索记忆、来源核验、取消运行、待确认记忆与应用操作卡片。项目空间可打开按需简报；日报与周报读取时展示来源，点击后才用已确认材料生成独立草稿，支持人工编辑和导出。升级需运行现有安装／修复流程，准备 Agent 依赖及数据库迁移；普通启动仍复用已有安装。

实现范围、数据保留、接口与开发验证见 [内置 Agent 实施说明](docs/embedded-agent.md)。

来源确认、报告草稿、桌宠反馈和自动备份的操作与边界见 [可靠采集与本机维护](docs/optimization.md)。
