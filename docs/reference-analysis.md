# 拾忆：参考项目源码分析与改进建议

分析日期：2026-10-09（Asia/Shanghai）。对象是当前工作区源码和 `references/` 中的五个上游浅克隆。上游版本以 [仓库快照清单](../references/repos.lock.json) 为准；本项目有未提交内容，因此现状判断针对工作区，不等同于某个已发布版本。本次进行静态源码分析，没有安装或运行参考项目，也没有重新执行本项目的模型验收。

详细实施顺序、估算和验收条件见 [开发规划](development-plan.md)。

本文的“现状”和缺口是开发前的分析快照，下面保留原始判断供对照。后续已实施 Web/桌宠共享 Pi、证据检索和工作整理等改进，并补充 Petex、vscode-pets 两个参考仓库；当前行为见 [个人版实施说明](personal-companion.md)，实际验证结果见 [验证记录](verification.md)。

## 1. 结论

拾忆已有值得保留的底座：PostgreSQL 事务与持久任务、离线文本队列、重要文字变化保护、记忆后端适配、来源版本核验、纠正与删除传播。当前主要短板在“把采集结果变成可用的工作上下文”：检索相关性会丢失、项目恢复只返回最后一条摘要、每日回顾只是片段列表、任务缺少跨片段归并。

建议吸收各家的具体方法，沿现有 Swift + FastAPI + PostgreSQL + Ollama + React 演进。首轮不增加新的数据库、云端模型、全量视频存储或第二套任务调度系统。最终体验目标是：**快速找回带证据的信息，恢复一个项目的工作状态，并在一天结束时得到可信、可修改的回顾。**

## 2. 本项目现状：已经具备与实际缺口

以下每项都能定位到当前代码；“建议”不代表已经实现。

| 编号 | 源码证据 | 现状与影响 | 建议 / 优先级 |
|---|---|---|---|
| L01 | [.gitignore](../.gitignore) 的 `models/`；[数据库模型](../backend/workmemory/db/models/__init__.py) | `git check-ignore -v backend/workmemory/db/models/__init__.py` 命中该规则，`git ls-files backend/workmemory/db/models` 为空。运行必需的模型源码没有进入当前 Git 文件清单，另一台机器仅拿 Git 内容会缺文件。 | 将模型下载目录规则限定到根目录 `/models/`，确认源码纳入版本管理并做干净导出检查。**P0**。本次只记录问题，未代替用户提交文件。 |
| L02 | [配置模型](../backend/workmemory/config/__init__.py) 的 `CollectorConfig`、[模板](../config/app.example.yaml)、[README](../README.md)、[OCR 文档](ocr.md) | 最终复核时，当前代码、模板和说明均为 `timer`、5 秒，`interaction` 是显式可选模式。该项已一致，不列为当前缺陷；不能用旧版操作触发说明估算当前成本。 | M0 保存当前默认值和两种模式的基线；后续优化同步核对配置升级与说明。**基线约束**。 |
| L03 | [采集入口](../macos/Sources/WorkMemory/main.swift) 的 `captureSnapshot()` / `processNext()`；[快照调度](../macos/Sources/WorkMemoryCore/SnapshotSchedule.swift) | 操作模式会查 OCR 缓存，定时模式每个有效 tick 直接 OCR。后台的跨时间桶去重能减少 LLM 任务，但不能节省前面的重复 OCR 和上传。 | 共享按窗口、图像精确指纹、OCR 配置版本分组的短期缓存，先降低静态页面成本，再评估更复杂的采样策略。**P1**。 |
| L04 | [检索服务](../backend/workmemory/assistant/service.py) 的 `retrieve()`；[ReMe 适配](../backend/workmemory/memory/reme_adapter.py) 的 `search()` | 后端返回的排名/分数被转换为 ID，随后 SQL 按 `started_at DESC` 截断。中文关键词以 OR 匹配；命中项目名时直接取该项目最近记录。旧的高度相关证据可能被新的弱相关记录挤掉。 | 保留后端排名，增加结构化过滤和业务全文检索，用排名融合后选证据；“最新状态”与“历史事实”采用不同时间策略。**P1**。 |
| L05 | [问答服务](../backend/workmemory/assistant/service.py) 的 `answer_question()`；[摘要服务](../backend/workmemory/episodes/service.py) 的 `bounded_context()` | 问答只使用至多 8 条摘要、每条至多 900 字；原文不是检索对象。摘要输入也有字符截断。来源 UUID 存在并不保证答案中每个事实都受其支持。 | 两层证据：长期摘要/结构化事实 + 保留期内的原文命中；按结论返回证据段，并保留有效版本复核、无证据拒答。**P1**。 |
| L06 | [入库](../backend/workmemory/ingestion/service.py) 的 `episode_key()`；[摘要](../backend/workmemory/episodes/service.py) 的 `Extraction` | 五分钟片段按设备和人工项目提示分桶，自动采集通常没有提示。同一桶里可包含多个任务；一个片段只归属一个项目。抽取只有 summary/project/todos。 | 保留五分钟片段作证据单位，在其上新增“工作段”和类型化事实；允许一个片段贡献给多个工作段，关联必须有观察记录依据。**P1**。 |
| L07 | [路由](../backend/workmemory/api/routes.py) 的 `resume_project()` / `review()`；[前端](../frontend/src/App.tsx) 的项目与回顾页面 | 恢复项目只把最后一条摘要写进提醒；回顾只返回当日 ready 片段。现有入口已可用，但尚无跨片段工作简报、决策变更、阻塞与下一步。 | 生成可缓存、可失效重建的项目简报和每日/每周回顾；页面直接展示，并逐项关联证据。**P1**。 |
| L08 | [摘要服务](../backend/workmemory/episodes/service.py) 的 `summarize_job()`；[Todo 模型](../backend/workmemory/db/models/__init__.py) | 待办唯一性仅为 `(episode_id, text)`；同一事项跨片段或换一种说法可重复生成。确认、完成和到期提醒已有明确状态。 | 增加项目级任务身份、多个来源、拒绝状态与相似候选；完成状态继续以用户确认等明确证据为准。**P1/P2**。 |
| L09 | [采集协议](../backend/workmemory/ingestion/service.py) 的 `Event`；[OCR 引擎](../ocr/workmemory_ocr/engine.py)；[来源接口](../backend/workmemory/api/routes.py) 的 `sources()` | 保存 app/title/window/phase，但没有浏览器 URL 或结构化 OCR 行置信度；来源详情主要围绕单个片段。 | 增加可选、经过清理的资源定位和 OCR 质量元数据，支持命中段高亮、前后邻居与采集缺口说明。**P2**。 |
| L10 | [模型调度](../backend/workmemory/inference/ollama.py)；[worker](../backend/workmemory/jobs/worker.py)；[两个适配器](../backend/workmemory/memory/) | 模型队列有优先级，但不抢占运行请求；适配器的同一把锁覆盖读写。长记忆写入可能阻塞检索；维护循环也要等当前 job 结束。 | 为检索设置可观测等待预算，超时后使用业务检索；批量嵌入、后台限额和独立维护调度仍沿用现有任务与锁约束。**P2**。 |
| L11 | [前端](../frontend/src/App.tsx) 的 `refresh()` 和 `setInterval()` | 主文件约 1500 行，所有页面每 10 秒刷新状态、时间线、项目、待办、提醒。项目卡片状态文本硬编码“进行中”。 | 随功能分解页面和数据请求；按页面、可见性刷新，展示真实项目状态，后续按需求引入事件推送。**P2**。 |
| L12 | [监控脚本](../scripts/monitor.py)、[既有验收记录](verification.md)、[中文样本](../tests/fixtures/chinese_work.json) | 已有性能采样和合成验收；缺少有标注的连续工作回放、大历史检索、采集遗漏与摘要事实保真评估。已有报告不能证明所有真实工作场景达标。 | 建立可重放、可标注的中文基准，再以同一数据集比较优化前后。**P0/P1**。 |

### 现有设计应继续保留

- [入库事务](../backend/workmemory/ingestion/service.py)、[任务租约](../backend/workmemory/jobs/queue.py)、[Spool](../macos/Sources/WorkMemoryCore/Spool.swift) 已处理幂等、离线重传和任务恢复；增加功能时复用。
- [差异判定](../backend/workmemory/ingestion/dedup.py) 保护金额、日期、否定词、状态和错误等小变化；不能为降低 OCR 次数而用全屏感知相似度直接替代。
- [问答有效性复核](../backend/workmemory/assistant/service.py)、[删除与纠正](../backend/workmemory/episodes/service.py)、[同步](../backend/workmemory/memory/sync.py) 把 PostgreSQL 当作最终事实来源；新增工作段、简报、全文索引、反馈和缓存都必须遵守同一失效规则。
- [Graphiti 片段隔离](../backend/workmemory/memory/graphiti_adapter.py) 是现有删除策略的一部分；首轮用业务证据关系实现跨片段上下文，不急于改成共享实体图。

## 3. 五个参考项目：具体吸收什么

### 3.1 MineContext：把上下文分成可以消费的结构

重点源码：

- [ContextType](../references/MineContext/opencontext/models/enums.py)（第 84 行起）与 [ExtractedData / ContextProperties](../references/MineContext/opencontext/models/context.py)：区分实体、活动、意图、语义、流程、状态与知识，保留原始来源列表。
- [截图处理器](../references/MineContext/opencontext/context_processing/processor/screenshot_processor.py)：`_is_duplicate()` 做感知哈希去重；`_merge_items_with_llm()` 按类型归并并合并原始来源；`batch_process()` 组织批量 VLM 处理。
- [报告生成器](../references/MineContext/opencontext/context_consumption/generation/generation_report.py)：先按小时生成中间摘要，再合成报告。
- [SmartTodoManager](../references/MineContext/opencontext/context_consumption/generation/smart_todo_manager.py)：`_deduplicate_with_vector_search()` 比较历史待办与同批待办；[ConsumptionManager](../references/MineContext/opencontext/managers/consumption_manager.py) 分别调度活动、建议、待办和报告。

**吸收方式**：在现有摘要抽取中增加进展、决策、阻塞、行动四种最小事实类型，不一次复制七种上下文体系；新增工作段/事实来源表，把日报和项目简报变成有证据依赖的派生结果。待办先在同项目范围按归一化文本去重，再提供语义合并建议。日报按工作段或小时增量生成，避免每天从所有原文重新推理。

**适配拾忆的取舍**：MineContext 的截图/VLM 路径和线程计时器不直接迁移；继续使用本地 OCR 文本和现有 PostgreSQL Job。它的独立 `context_merger` 在 [默认配置](../references/MineContext/config/config.yaml) 中是 `enabled: false`，要区别于截图处理器内部已调用的批次合并，不能把所有高级合并都当作默认运行能力。其待办去重函数在嵌入失败时有 `continue` 分支，拾忆应保留待确认候选并标明未完成去重，不能让模型服务短暂故障吞掉任务。

对应改进：L06、L07、L08；开发任务 D07–D10。

### 3.2 Screenpipe：采集状态、OCR 成本和工具边界的对照

重点源码：

- [事件采集实现](../references/screenpipe/crates/screenpipe-engine/src/event_driven_capture.rs) 与 [paired_capture](../references/screenpipe/crates/screenpipe-capture/src/paired_capture.rs)：协调画面、当前应用/窗口与辅助功能数据；包含正文不足时的 OCR 判定及 OCR 质量/缓存指标。
- [OcrGate](../references/screenpipe/crates/screenpipe-capture/src/ocr_gate.rs)：区分待处理签名和已持久化签名；只有确认结果已存储后才把相同内容作为可跳过状态。文件内有失败重试、像素变化和缓存复用检查。
- [电源档位](../references/screenpipe/crates/screenpipe-engine/src/power/profile.rs)：把电池/节能状态映射为一组采集参数，而非分散修改多个计时器。
- [工具权限](../references/screenpipe/crates/screenpipe-core/src/pipes/permissions.rs)、[API 中间件](../references/screenpipe/crates/screenpipe-engine/src/pipe_permissions_middleware.rs)、[MCP](../references/screenpipe/packages/screenpipe-mcp/src/index.ts)：提供应用、窗口、内容、时间与 API 范围控制。

**对拾忆的启发**：统一记录“检测到变化、执行 OCR、缓存命中、文本入队、服务确认”这些不同状态。缓存识别成功与记录交付成功分开：已识别文本可以省 OCR，但不能因为缓存命中就跳过尚未成功的入队/重传。优先做完整窗口精确缓存；文本区域裁剪和检测器成本作为后续实验。资源策略集中配置，降频原因在状态页可见。

**适配取舍**：拾忆目前明确以 PaddleOCR 为正文来源，不能因为对方 AX 优先就默默改变默认或在 OCR 故障时切换；若实验 AX 辅助采集，需按应用验证正文覆盖与敏感输入处理。Screenpipe 的 [事件采集设计文档](../references/screenpipe/docs/EVENT_DRIVEN_CAPTURE_SPEC.md) 自身标注 Historical，本文以上游实现为准，不搬用其中旧性能数字。

当前快照使用 [Screenpipe Commercial License](../references/screenpipe/LICENSE.md)，文件明确包含商业集成及竞争产品限制。本规划把它作为产品和实现对照，**不把当前版本列为可直接复制代码的候选**；后续如考虑集成或摘取代码，需要单独核对所用版本及授权范围。当前没有将任何参考项目代码接入拾忆。

对应改进：L03、L09、L10；开发任务 D03、D12、D13。

### 3.3 OpenRecall：简单流程与反例基线

重点源码：

- [screenshot.py](../references/openrecall/openrecall/screenshot.py)：`is_similar()` 使用图像结构相似度，采集循环先检查用户活动。
- [utils.py](../references/openrecall/openrecall/utils.py)：各系统的活动状态与当前窗口获取。
- [app.py](../references/openrecall/openrecall/app.py) 的 `search()`：读取历史条目、计算相似度并排序；[nlp.py](../references/openrecall/openrecall/nlp.py) 展示基础嵌入与余弦相似度流程。

**吸收方式**：把简单的“用户活跃检查”和“极简检索流程”用作评估基线。在拾忆中，空闲、暂停和窗口不可采集要区分；省电策略可以降低采样，但必须保留用户可见的缺口解释。完整实现仍使用现有本地队列、持久任务和来源核验。

**不适合直接移植之处**：全量条目载入内存再计算相似度不适合作为大历史数据方案；全屏相似度阈值可能掩盖一个数字或否定词的变化。当前克隆的 `screenshot.py` 在第 107 和 167 行重复定义 `record_screenshots_thread`，后一个定义覆盖前一个，并引用该函数中未定义的 `current_screenshot` 和 `filename`；调用参数也与 `database.insert_entry()` 的签名不一致。这些是静态源码发现，未运行复现，足以说明不能把该快照视为可直接依赖的采集模板。

对应改进：L03、L12；用于 D02 / D03 的基线和边界场景。

### 3.4 Dayflow：工作段、连续时间线与可编辑回顾

重点源码：

- [AnalysisManager](../references/Dayflow/Dayflow/Dayflow/Core/Analysis/AnalysisManager.swift) 的 `createScreenshotBatches()`：按持续时间和观测间隔分批；[IdleBatchClassifier](../references/Dayflow/Dayflow/Dayflow/Core/Analysis/IdleBatchClassifier.swift) 单独处理空闲判断。
- [LLMService](../references/Dayflow/Dayflow/Dayflow/Core/AI/LLMService.swift) 的 `processBatch()`（滑动窗口部分约第 834 行）：先保存 observations，再读取近期观察与现有卡片，生成能衔接的活动卡片；卡片生成与替换有串行互斥。
- [TimelineOutputSupport](../references/Dayflow/Dayflow/Dayflow/Core/AI/TimelineOutputSupport.swift)：`validateTimeCoverage()` / `validateTimeline()` 对时间范围与输出做确定性检查。
- [DailyRecapGenerator](../references/Dayflow/Dayflow/Dayflow/Core/AI/DailyRecapGenerator.swift)、[DailyRecapScheduler](../references/Dayflow/Dayflow/Dayflow/Core/AI/DailyRecapScheduler.swift)、[DailyRecapAttemptBudget](../references/Dayflow/Dayflow/Dayflow/Core/AI/DailyRecapAttemptBudget.swift)：区分生成、触发条件与持久化尝试预算。本地生成路径输出完成事项和下一步，并明确要求不猜测缺失日志。
- [WeeklyDashboardBuilder](../references/Dayflow/Dayflow/Dayflow/Core/Weekly/WeeklyDashboardBuilder.swift)、[TimelineReviewCard](../references/Dayflow/Dayflow/Dayflow/Views/UI/TimelineReviewCard.swift)、[DailyView+Standup](../references/Dayflow/Dayflow/Dayflow/Views/UI/DailyView+Standup.swift)：活动分类、时间统计、回顾和编辑界面的具体分层。

**吸收方式**：在五分钟 Episode 上方新增 WorkSession。新片段到达时只重算最近一段时间的工作段，带上既有段落、明确项目与采集间隔，再用确定性逻辑核验时间和来源。较早稳定段不反复调用模型；项目详情展示“进展—决策—阻塞—下一步”，来源可展开。日报从这些工作段生成，用户能调整分类、修改正文和确认下一步。

对连续聚合应增加 **最大回看窗口、最大证据数、最大调用次数**，避免每次新采集都重新处理整个项目。Dayflow 的自动日报预算按目标日持久化，即使重启也不重置；拾忆应把同样的产品约束落在现有 PostgreSQL 中，按日/产物限制尝试次数，不能另加一套独立状态。

**适配取舍**：Dayflow 的观察结果主要来自其截图/模型管线，不能把它的观察生成成本当作拾忆 OCR 的成本。滑动窗口只重算派生工作段，不能覆盖原始证据。其时间线覆盖规则不能用来“填满”拾忆没有采到的时段；保留暂停和遗漏。其部分日志会打印 observation，拾忆已有不记录正文的约束，不迁移这种调试输出。UI 参考信息结构和交互，不需要把 React 改为 SwiftUI，也不复制品牌素材。

对应改进：L06、L07、L10、L11；开发任务 D07–D09、D11、D12。

### 3.5 Pensieve：检索排名、前后文浏览与按负载安排处理

重点源码：

- [搜索实现](../references/pensieve/memos/search.py) 的 `PostgreSQLSearchProvider.full_text_search()`、`vector_search()`、`reciprocal_rank_fusion()`、`hybrid_search()`：中文查询先用 jieba 分词，PostgreSQL 路径采用 `simple` 文本配置与 `ts_rank_cd`；全文和向量候选分别按时间、应用、库过滤，再按排名融合。当前 RRF 的全文/向量权重为 0.7/0.3，`k=60`，这些只是该快照的参数。
- [搜索 API](../references/pensieve/memos/server.py) 的 `search_entities_v2()`：把日期桶与显式时间窗口取交集，区分匹配计数和集合规模，记录嵌入、全文、向量及融合耗时。混合搜索与计数/统计使用不同数据库会话执行；不能把这一点理解成其所有检索步骤都并行。
- 同一文件的 `get_entity_context()` 提供指定条目的前后记录；[ContextNavigationBar](../references/pensieve/web/src/components/entity/ContextNavigationBar.tsx) 和 [HitViewerModal](../references/pensieve/web/src/components/entity/HitViewerModal.tsx) 区分“按搜索结果翻页”和“展开时间上下文”。
- [文件监听](../references/pensieve/memos/cmds/library.py) 的 `update_processing_interval()` 根据文件到达速率与处理吞吐调整抽样间隔，电池模式进一步稀疏化；`process_unprocessed_files()` 在空闲、供电与允许时段条件下补处理积压，每个条目前重新检查状态。[watch_state.py](../references/pensieve/memos/utils/watch_state.py) 集中实现电池和跨午夜时间窗口判断。

**吸收方式**：优先改造拾忆的检索。业务 PostgreSQL 增加中文全文索引，ReMe / Graphiti 继续提供语义候选；保留两路名次，完成结构化过滤、RRF 和来源版本校验后再截断。不要照搬 0.7/0.3 权重，用中文基准选择参数。搜索页增加日期、项目和应用筛选；来源抽屉支持前后邻居、匹配文字和记录缺口。分项耗时能区分“等待后台写入锁”“生成查询向量”“业务检索”各自的成本。

资源调度只借鉴按输入/处理速率调整后台预算的思路：在拾忆中使用现有持久任务、可观测积压和用户活动信号，优先处理问答，空闲时批量补索引和生成回顾。保留期清理应独立于重模型任务，不能因等待空闲或接电而延期。

**适配取舍**：Pensieve 的 `processing_interval` 在 `process_pending_files()` 中是“每 N 个文件运行一次插件”，不是截图秒数；watcher 的 idle 由最近文件事件时间判断，不能当成操作系统已经确认用户离开。它能依靠落盘文件稍后补处理；拾忆截图不落盘，不能跳过 OCR 后声称还能补识别。可延后的应是已可靠保存的文本处理。

其 PostgreSQL 向量检索依赖数据库向量类型，拾忆不引入该存储路线。当前全文检索先对匹配集合做 `LIMIT FTS_RANK_CAP`，再排序，该近似截断可能漏掉高相关候选；拾忆应先保证标注集的召回，再决定如何限时或截断，并让搜索计数清楚区分精确值、下限与返回候选数。这里采用检索设计，未直接复制其数据库或 API 实现。

对应改进：L04、L05、L09、L10、L11；开发任务 D04–D06、D11、D12。

## 4. 方法如何组合到拾忆

| 优先级 | 参考方法 | 拾忆中的落点 | 预期用户收益 | 关键取舍 |
|---|---|---|---|---|
| P0 | 所有项目对照后的可复现性与基准检查 | D01–D02：源码纳管、默认配置基线、中文回放 | 换机可运行，优化效果可比较 | 当前默认文档已一致；数据库模型被忽略的问题仍需修复。 |
| P1 | Screenpipe 的识别/交付状态区分；OpenRecall 的活动检查基线 | D03：共享精确缓存、失败重试与缺口统计 | 静态页面减少重复 OCR，关键小变化仍保留 | 用独立实现，不直接采用 Screenpipe 当前受限代码；不用全屏相似度丢弃文字变化。 |
| P1 | Pensieve 的混合检索、过滤与上下文浏览 | D04–D06：中文全文 + 后端排名 + 证据包 | 能找到更早的相关记录，也能核对原文 | 保留业务数据库核验；框架返回的文字不能直接成为事实。 |
| P1 | MineContext 的事实类型与来源；Dayflow 的滚动工作段 | D07–D08：事实、工作段、项目简报 | 恢复项目时看到进展、决策、阻塞与下一步 | 工作段是可重建结果，五分钟证据不被改写；缺失时段不补成工作时间。 |
| P1/P2 | MineContext 分层报告/任务归并；Dayflow 回顾与尝试预算 | D09–D11：日报周报、可编辑任务、页面按需加载 | 回顾可以直接使用，同一任务不反复打扰 | 保留用户确认、拒绝和编辑状态；生成预算重启后不重置。 |
| P2 | Pensieve 吞吐反馈、Screenpipe 电源策略与只读工具范围 | D12–D13：后台限额、独立维护、可选资源定位/MCP | 减少交互等待，让已有记忆能按授权被其他工具读取 | 不保证抢占已运行模型；外部工具仍走业务来源和权限核验。 |

建议先交付 D01–D06，再做跨片段理解。只增加上下文类型和更长提示词，无法修复当前检索把旧证据挤掉的问题；先把证据入口做稳，项目简报和回顾才能依赖它。

## 5. 版本与许可记录

以下记录来自本次克隆根目录的许可文件，具体 SHA 和许可文件摘要见 [快照清单](../references/repos.lock.json)。这是源码引用范围的记录；依赖、模型和素材可能有各自许可，当前没有将这些内容集成到拾忆。

| 项目 | 当前快照根许可 | 本规划的使用方式 |
|---|---|---|
| MineContext | [Apache-2.0](../references/MineContext/LICENSE) | 参考上下文结构和生成流程；如实际摘取代码，记录具体文件与来源。 |
| Pensieve | [Apache-2.0](../references/pensieve/LICENSE) | 优先参考检索和 UI 交互；适配现有业务数据模型。 |
| Dayflow | [MIT](../references/Dayflow/LICENSE) | 参考工作段与回顾；不复制品牌素材。 |
| OpenRecall | [AGPL v3](../references/openrecall/LICENSE) | 作为简单算法与边界场景的对照，本轮无代码集成。 |
| Screenpipe | [Screenpipe Commercial License](../references/screenpipe/LICENSE.md) | 仅作源码/产品对照，不列为直接复制代码候选；不能沿用历史 MIT 标签描述本次快照。 |

## 6. 本次验证范围

已核对五个仓库的 origin、HEAD、浅克隆标记和工作区状态；具体结果保存在参考目录清单。源码分析通过函数和文件交叉对照形成，规划指标都是待测目标。

未安装上游依赖、未运行其应用、未进行性能横评；没有执行本项目的业务测试或真实桌面采集验收。本次交付只新增参考克隆、清单与分析/规划文档，业务能力仍以现有代码为准。
