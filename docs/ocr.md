# 每五秒采集与 PaddleOCR

默认使用已锁定的 PaddleOCR **3.7.0**、PaddlePaddle **3.3.1**、PaddleX **3.7.2**，检测和识别均为 **PP-OCRv6 tiny**。依赖见 `ocr/uv.lock`，模型及文件 SHA-256 在 `models/paddleocr/inventory.json`。small 模型可以在本地配置中手动选择。

AX 用于应用、焦点窗口、标题和安全输入状态，不再以“AX 字数够了”为由跳过 OCR。`ocr_mode: off` 是明确选择的 AX 文字模式；兼容旧配置的 `fallback` 在新采集器中也使用 OCR，不在 OCR 故障时自动改用 AX。

## 默认采集过程

```text
每 5 秒 → 当前窗口单张内存截图 → PaddleOCR tiny
    ↓ 串行；上一轮未完成就跳过，不补采
文本文件队列 → PostgreSQL
    ↓ 95% 相似度 + 重要变化保护 + 跨片段去重
有实质变化的正文 → 每个五分钟片段一个可合并任务
    ↓
Ollama 摘要 → 当前记忆后端 → 证据问答与回顾
```

- 定时模式使用单次截图，不维持连续视频流，不监听 Enter 或鼠标点击，也不生成前后画面组。只需要辅助功能与屏幕录制权限，无需输入监控。
- 每五秒尝试一次采样，OCR 开始间隔至少五秒；长时间识别占用时跳过到期采样，恢复后只处理新的窗口画面。实际间隔可能超过五秒。
- PNG 最大 8 MB、画面最大 2400×1800；识别服务拒绝超过 450 万像素的图像。截图不落盘，不记录图像正文日志。应用 PNG 缓存字节数不包括系统图像缓冲或模型 RSS。
- 锁屏、睡眠或暂停时停止采集；排除的应用、标题和安全输入框不采集。截图前后核验前台窗口，在途结果失效时不入库。
- OCR 不可用时明确显示异常。OCR 已完成的文字由默认 50 MB 文件队列保证上传重试，确认入库或过滤后删除；队列满时暂停，不覆盖旧批次。

## 夜间去重与重要变化

定时采集在同设备、应用、窗口、标题和项目范围内，比较最近保留的文本及最近有效完整基准，防止连续小改动一直被过滤。该基准来自 PostgreSQL，重启采集端或 API 不会清空。

归一化字符编辑相似度达到 95% 时，默认不增加大模型输入；数字、金额、日期、否定词、完成状态、错误信息以及无法解释的新增/删除句子仍保留。词表使用可配置正则表达式，规则判断不等同于全面的语义理解。

**跨五分钟边界同样去重。** 电脑整晚停在相同页面时，后续定时样本不创建新片段，也不产生新的摘要或记忆模型任务；已有待处理任务仍会正常完成。同片段内重复观察保留元数据，近似重复保存直接引用完整基准的可逆差异。跨片段的无实质变化样本直接确认过滤，避免产生整晚的空时间线。

有实质变化才更新摘要版本；正在整理时出现的新版本会在完成后继续排队。原始正文、差异和窗口标题最多保留 30 天，纠正或删除同时清除基准和差异。过期基准不用于后续去重，因此保留期届满后需要重新建立基准。

## 可选操作触发模式

手动设置 `capture_mode: interaction` 可保留操作前后采集：Enter、点击、应用/窗口切换及稳定画面变化触发，连续操作合并。此模式才需要输入监控权限，只识别 Enter 键码和点击，不记录普通按键内容。

内存取样上限 4 帧/秒，操作前画面取自此前 500 毫秒；静默合并 600 毫秒，稳定至少 500 毫秒后取后画面，最长 2 秒一组。最多 3 组待处理；64 MiB PNG 队列预算中预留 16 MiB 给帧传递，系统缓冲另计。积压按整组合并或淘汰并计数，缺失画面不会伪装成已采集。跨五分钟边界的前后画面按操作发生时间归到同一片段。

## 配置和验证

```yaml
collector:
  capture_mode: timer       # interaction 为可选操作触发模式
  interval_seconds: 5
  ocr_mode: always          # off 为手动 AX 模式
  ocr_min_interval: 5
  similarity_threshold: 0.95
ocr:
  detection_model: PP-OCRv6_tiny_det
  recognition_model: PP-OCRv6_tiny_rec
  cpu_threads: 2
```

管理器以 nice 10 的后台优先级启动 OCR 进程，减少与桌面交互竞争；这不会降低图片精度、线程数或采集频率，也不代表模型常驻内存消失。调整后应使用合成基准及真实采集监测分别验证，不能根据空闲 CPU 推断繁忙窗口的性能。

修改配置后选择管理菜单的安装／修复。权限操作见 `docs/management.md`。

```bash
swift run --package-path macos InteractionCheck
swift run --package-path macos SpoolCheck
ocr/.venv/bin/python scripts/verify_ocr.py
ocr/.venv/bin/python scripts/benchmark_ocr.py
uv run python scripts/monitor.py --seconds 1800
```

`InteractionCheck` 包含五秒定时、慢识别跳过、睡眠后不补采及可选操作队列测试。PostgreSQL 集成测试覆盖模拟八小时静止页面、跨片段近似重复、并发上传、累积修改和重要变化。

`verify_ocr.py` 是简单合成文字的识别和入库验证；`benchmark_ocr.py` 是网页、聊天、编辑器、文档密度的合成图像测试。二者都不读取个人桌面，也不能证明真实窗口权限、识别率或长期采集性能。

`monitor.py` 默认持续 30 分钟，只采样无正文的延迟、队列、丢弃、进程内存和 CPU 数据，写入 `data/exports/collector-monitor.json`。需要先授权并开始采集，正常使用真实应用后才能评估性能。无法采集时报告 `capture_verified: false`。
