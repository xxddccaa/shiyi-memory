import AppKit
import ApplicationServices
import WorkMemoryCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var desktopPet: DesktopPet!
    private var toggle: NSMenuItem!
    private var state: NSMenuItem!
    private var queueState: NSMenuItem!
    private var ocrState: NSMenuItem!
    private var config: CollectorConfig!
    private var spool: TextSpool!
    private var ocrClient: OCRClient!
    private var buffer: InteractionBuffer!
    private var input: InputEvents!
    private var focusNotifications: FocusNotifications!
    private var stream: FrameStream?
    private var context: FocusedContext?
    private var timer: Timer?
    private var snapshotSchedule = SnapshotSchedule()
    private var signalSource: DispatchSourceSignal?
    private var captureTask: Task<Void, Never>?
    private var deviceID = ""
    private var consent = false
    private var desiredRunning = false
    private var locked = false
    private var refreshing = false
    private var processing = false
    private var uploading = false
    private var inputReady = false
    private var generation = 0
    private let processID = UUID().uuidString
    private var stage = "idle"
    private var skipReason: String?
    private var queueFull = false
    private var uploadFailureSince: String?
    private var screenshots = 0
    private var screenshotFailures = 0
    private var ocrAttempts = 0
    private var emptyResults = 0
    private var uploadFailures = 0
    private var uploadAccepted = 0
    private var uploadFiltered = 0
    private var lastScreenshotAt: String?
    private var lastUploadAt: String?
    private var lastProgressAt = ISO8601DateFormatter().string(from: Date())
    private var captureCount = 0
    private var ocrCount = 0
    private var ocrFailures = 0
    private var snapshotImageBytes = 0
    private var spooled = 0
    private var lastStatus = 0.0
    private var lastRefresh = 0.0
    private var lastUpload = 0.0
    private var lastOCRStart = -100.0
    private var lastCaptureAt: String?
    private var lastOCRAt: String?
    private var lastOCRSeconds: Double?
    private var lastMethod: String?
    private var lastError: String?
    private let ocrCache = OCRCache()
    private var ocrSignature: String { (config.ocrEndpoint ?? "local") + ":" + (config.ocrSignature ?? "default") }
    private var recentOCR: [Double] = []
    private var uptime: Double { ProcessInfo.processInfo.systemUptime }
    private var enabled: Bool { consent && desiredRunning && !locked && !queueFull && ProcessInfo.processInfo.thermalState != .critical }
    private var previousThermal: ProcessInfo.ThermalState = .nominal
    private var interaction: Bool { (config.captureMode ?? "timer") == "interaction" }
    private var usesOCR: Bool { (config.ocrMode ?? "always") != "off" }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let configPath = ProcessInfo.processInfo.environment["WORKMEMORY_CONFIG"] ?? Bundle.main.object(forInfoDictionaryKey: "WorkMemoryConfigPath") as? String
            guard let path = configPath else { throw CocoaError(.fileNoSuchFile) }
            config = try JSONDecoder().decode(CollectorConfig.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
            snapshotSchedule = SnapshotSchedule(interval: max(config.intervalSeconds, config.ocrMinInterval ?? 5))
            guard let endpoint = URL(string: config.endpoint), ["127.0.0.1", "localhost"].contains(endpoint.host ?? "") else { throw OCRError.invalidEndpoint }
            ocrClient = try OCRClient(endpoint: config.ocrEndpoint ?? "http://127.0.0.1:8766", token: config.token, timeout: config.ocrTimeoutSeconds ?? 45)
            spool = try TextSpool(directory: URL(fileURLWithPath: config.spoolPath), maxBytes: config.maxSpoolBytes)
            buffer = InteractionBuffer(merge: config.mergeSeconds ?? 0.6, maximum: config.maxGroupSeconds ?? 2,
                stable: config.stableSeconds ?? 0.5, budget: max(16 * 1024 * 1024, (config.imageBudgetBytes ?? 64 * 1024 * 1024) - 16 * 1024 * 1024), maxPending: config.maxPendingGroups ?? 3)
            let identity = URL(fileURLWithPath: config.spoolPath).deletingLastPathComponent().appendingPathComponent("device-id")
            if let id = try? String(contentsOf: identity, encoding: .utf8) { deviceID = id }
            else { deviceID = UUID().uuidString; try deviceID.write(to: identity, atomically: true, encoding: .utf8) }
            if let path = config.captureStatePath, let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
               let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Bool] {
                consent = saved["consent"] == true; desiredRunning = consent && saved["running"] == true
            }
        } catch {
            let alert = NSAlert(); alert.messageText = "采集端配置不可用"
            alert.informativeText = "请双击项目中的“拾忆管理.command”，选择安装／修复。"; alert.runModal(); NSApp.terminate(nil); return
        }
        desktopPet = DesktopPet(endpoint: config.endpoint, token: config.token, dataDirectory: URL(fileURLWithPath: config.spoolPath).deletingLastPathComponent())
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "leaf", accessibilityDescription: "拾忆采集")
        let menu = NSMenu()
        state = NSMenuItem(title: "等待就绪", action: nil, keyEquivalent: "")
        queueState = NSMenuItem(title: "本地待上传：0", action: nil, keyEquivalent: "")
        ocrState = NSMenuItem(title: "PaddleOCR tiny · 每 \(Int(snapshotSchedule.interval)) 秒采集", action: nil, keyEquivalent: "")
        menu.addItem(state); menu.addItem(queueState); menu.addItem(ocrState); menu.addItem(.separator())
        toggle = NSMenuItem(title: desiredRunning ? "暂停采集" : "开始采集", action: #selector(toggleCapture), keyEquivalent: "")
        toggle.target = self; menu.addItem(toggle)
        add(menu, "打开工作空间", #selector(openDashboard))
        add(menu, "和桌宠聊天", #selector(openPetChat))
        add(menu, "显示／隐藏桌宠", #selector(togglePet))
        menu.addItem(.separator())
        add(menu, "授予辅助功能权限", #selector(requestAX))
        add(menu, "授予屏幕录制权限", #selector(requestScreen))
        if interaction { add(menu, "授予输入监控权限（操作触发）", #selector(requestInput)) }
        menu.addItem(.separator()); add(menu, "退出拾忆（含桌宠与采集）", #selector(quit)); item.menu = menu
        input = InputEvents { [weak self] kind, time in
            MainActor.assumeIsolated { self?.trigger(kind, at: time) }
        }
        focusNotifications = FocusNotifications { [weak self] in
            Task { @MainActor in await self?.refreshContext() }
        }
        let workspace = NSWorkspace.shared.notificationCenter
        workspace.addObserver(self, selector: #selector(applicationChanged), name: NSWorkspace.didDeactivateApplicationNotification, object: nil)
        workspace.addObserver(self, selector: #selector(applicationChanged), name: NSWorkspace.didActivateApplicationNotification, object: nil)
        workspace.addObserver(self, selector: #selector(suspendCapture), name: NSWorkspace.willSleepNotification, object: nil)
        workspace.addObserver(self, selector: #selector(resumeCapture), name: NSWorkspace.didWakeNotification, object: nil)
        workspace.addObserver(self, selector: #selector(suspendCapture), name: NSWorkspace.sessionDidResignActiveNotification, object: nil)
        workspace.addObserver(self, selector: #selector(resumeCapture), name: NSWorkspace.sessionDidBecomeActiveNotification, object: nil)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(suspendCapture), name: NSNotification.Name("com.apple.screenIsLocked"), object: nil)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(resumeCapture), name: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil)
        signal(SIGTERM, SIG_IGN)
        signalSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        signalSource?.setEventHandler { [weak self] in self?.quit() }; signalSource?.resume()
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        tick()
        if CommandLine.arguments.contains("--pet-chat") { desktopPet.openChat() }
    }
    private func add(_ menu: NSMenu, _ title: String, _ selector: Selector) {
        let entry = NSMenuItem(title: title, action: selector, keyEquivalent: ""); entry.target = self; menu.addItem(entry)
    }
    private func excluded(_ title: String) -> Bool {
        title.contains("拾忆") || config.excludedTitlePatterns.contains { title.range(of: $0, options: .regularExpression) != nil }
    }
    private func persistChoice() {
        guard let path = config.captureStatePath,
              let data = try? JSONSerialization.data(withJSONObject: ["consent": consent, "running": desiredRunning]) else { return }
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
    }
    @objc private func toggleCapture() {
        if !consent {
            let alert = NSAlert(); alert.messageText = "允许拾忆记录工作上下文？"
            alert.informativeText = (interaction ? "采集当前窗口在操作前后的文字，只识别 Enter 和点击，不记录普通按键内容。" : "每 \(Int(snapshotSchedule.interval)) 秒识别一次当前窗口文字；重复内容不触发新的大模型整理任务。") + "截图只在内存中短暂处理，原始文本保留30天。你可以随时在菜单栏暂停。"
            alert.addButton(withTitle: "同意并开始"); alert.addButton(withTitle: "暂不开始")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            consent = true
        }
        desiredRunning.toggle(); persistChoice(); lastError = nil; lastProgressAt = ISO8601DateFormatter().string(from: Date())
        if !desiredRunning { invalidate() }
        toggle.title = desiredRunning ? "暂停采集" : "开始采集"
        Task { await refreshContext() }; writeStatus()
    }
    @objc private func requestAX() {
        _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    }
    @objc private func requestScreen() { _ = CGRequestScreenCaptureAccess(); writeStatus() }
    @objc private func requestInput() {
        _ = CGRequestListenEventAccess()
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent") { NSWorkspace.shared.open(url) }
    }
    @objc private func openPetChat() { desktopPet.openChat() }
    @objc private func togglePet() { desktopPet.toggleVisibility() }
    @objc private func openDashboard() {
        var url = URLComponents(string: config.endpoint)!; url.fragment = "token=" + config.token
        if let url = url.url { NSWorkspace.shared.open(url) }
    }
    @objc private func applicationChanged(_ notification: Notification) {
        if notification.name == NSWorkspace.didDeactivateApplicationNotification && enabled { trigger("app_switch", at: uptime) }
        Task { await refreshContext() }
    }
    @objc private func suspendCapture() { locked = true; invalidate(); writeStatus() }
    @objc private func resumeCapture() { locked = false; lastProgressAt = ISO8601DateFormatter().string(from: Date()); Task { await refreshContext() } }
    @objc private func quit() {
        // Exiting/stopping services does not overwrite the user's capture preference.
        desiredRunning = false; invalidate(); writeStatus(); NSApp.terminate(nil)
    }
    private func invalidate() {
        generation += 1; buffer?.clear(); ocrCache.removeAll(); captureTask?.cancel()
        input?.stop(); inputReady = false; focusNotifications?.stop(); context = nil
        if let old = stream { stream = nil; Task { await old.stop() } }
    }
    private func tick() {
        let now = uptime
        let thermal = ProcessInfo.processInfo.thermalState
        if thermal != previousThermal {
            previousThermal = thermal
            if thermal == .critical { invalidate() }
        }
        let interval = (desktopPet?.resourcePolicy == "saving" || ProcessInfo.processInfo.isLowPowerModeEnabled) ? max(10, config.intervalSeconds, config.ocrMinInterval ?? 5) : max(config.intervalSeconds, config.ocrMinInterval ?? 5)
        if snapshotSchedule.interval != interval { snapshotSchedule.updateInterval(interval) }
        buffer.finishDue(at: now)
        if interaction && now - lastRefresh >= 0.5 { lastRefresh = now; Task { await refreshContext() } }
        if now - lastUpload >= 2 { lastUpload = now; Task { await upload() } }
        if enabled && !interaction && snapshotSchedule.take(at: now, busy: processing) {
            processing = true
            captureTask = Task { await captureSnapshot() }
        }
        if enabled && interaction && !processing && !buffer.pending.isEmpty {
            captureTask = Task { await processNext() }
        }
        if now - lastStatus >= 1 { lastStatus = now; writeStatus() }
    }
    private func trigger(_ kind: String, at time: Double) {
        guard interaction, enabled, AXIsProcessTrusted(), context != nil else { return }
        if usesOCR {
            guard stream != nil else { return }
            buffer.trigger(kind, at: time, date: Date(timeIntervalSinceNow: time - uptime),
                window: kind == "app_switch" || kind == "window_switch" ? nil : context.map { $0.app + ":" + String($0.window) })
        } else if !processing { captureTask = Task { await captureAXOnly(kind: kind) } }
    }
    private func refreshContext() async {
        guard !refreshing else { return }; refreshing = true
        defer { refreshing = false }
        guard enabled else { return }
        guard AXIsProcessTrusted() else { lastError = "accessibility_permission_required"; invalidate(); return }
        guard !usesOCR || CGPreflightScreenCaptureAccess() else { lastError = "screen_recording_permission_required"; invalidate(); return }
        guard let app = NSWorkspace.shared.frontmostApplication, let bundle = app.bundleIdentifier,
              !bundle.hasPrefix("local.workmemory."), !config.excludedApps.contains(bundle) else {
            skipReason = "excluded_app"; if context != nil { invalidate() }; return
        }
        let stamp = generation
        let pid = app.processIdentifier
        let focused = await Task.detached(priority: .utility) { focusedContext(pid: pid, bundle: bundle) }.value
        guard enabled, stamp == generation, NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { return }
        guard let focused, !focused.secure, !excluded(focused.title) else {
            skipReason = focused == nil ? "no_window" : focused!.secure ? "secure_input" : "excluded_title"
            if context != nil { invalidate() }; return
        }
        skipReason = nil
        if interaction && !inputReady {
            inputReady = input.start()
            if !inputReady { lastError = "input_monitoring_permission_required" }
        }
        if focused == context && (!usesOCR || !interaction || stream != nil) {
            if ["accessibility_permission_required", "screen_recording_permission_required", "input_monitoring_permission_required"].contains(lastError ?? "") && (inputReady || !interaction) { lastError = nil }
            return
        }
        if context != nil && buffer.active?.trigger != "app_switch" {
            trigger(context?.pid == focused.pid ? "window_switch" : "app_switch", at: uptime)
        }
        if let old = stream { stream = nil; await old.stop() }
        guard enabled, stamp == generation else { return }
        context = focused
        if !interaction { lastError = nil; return }
        focusNotifications.watch(pid: focused.pid)
        guard usesOCR else { return }
        let next = FrameStream(context: focused, deliver: { [weak self] frame in
            guard let owner = self else { return }
            await MainActor.run {
                guard owner.enabled, owner.generation == stamp, owner.context == focused,
                      NSWorkspace.shared.frontmostApplication?.processIdentifier == focused.pid else { return }
                owner.screenshots += 1; owner.lastScreenshotAt = ISO8601DateFormatter().string(from: Date()); owner.lastProgressAt = owner.lastScreenshotAt!
                owner.buffer.receive(frame, visual: owner.interaction)
            }
        }, failed: { [weak self] in Task { @MainActor in self?.lastError = "screen_stream_failed"; self?.invalidate() } })
        stream = next
        do {
            try await next.start(fps: config.frameRate ?? 4)
            if generation != stamp || !enabled { await next.stop(); return }
            if inputReady || !interaction { lastError = nil }
        } catch { stream = nil; lastError = "screen_stream_failed" }
    }
    private func captureSnapshot() async {
        defer { snapshotImageBytes = 0; processing = false; stage = "idle"; writeStatus() }
        await refreshContext()
        guard enabled, !Task.isCancelled, let context else { return }
        let stamp = generation
        if !usesOCR {
            processing = false
            await captureAXOnly(kind: "timer")
            return
        }
        do {
            let beforeResource = desktopPet.resourceCapture ? browserResource(pid: context.pid, bundle: context.app) : nil
            stage = "screenshot"
            guard let frame = try await captureWindowFrame(context), enabled, stamp == generation,
                  self.context == context, !Task.isCancelled else { return }
            let resource = desktopPet.resourceCapture && beforeResource == browserResource(pid: context.pid, bundle: context.app) ? beforeResource : nil
            screenshots += 1; lastScreenshotAt = ISO8601DateFormatter().string(from: Date()); lastProgressAt = lastScreenshotAt!
            snapshotImageBytes = frame.png.count
            let delay = max(1, config.ocrMinInterval ?? 5) - (uptime - lastOCRStart)
            if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            guard enabled, stamp == generation, self.context == context, !Task.isCancelled,
                  NSWorkspace.shared.frontmostApplication?.processIdentifier == context.pid,
                  focusedContext(pid: context.pid, bundle: context.app) == context else { return }
            var text = ocrCache.text(for: frame, signature: ocrSignature)
            if text == nil {
                lastOCRStart = uptime
                stage = "ocr"; ocrAttempts += 1
                let result = try await ocrClient.recognize(png: frame.png)
                guard enabled, stamp == generation, self.context == context, !Task.isCancelled,
                      NSWorkspace.shared.frontmostApplication?.processIdentifier == context.pid,
                      focusedContext(pid: context.pid, bundle: context.app) == context else { return }
                ocrCount += 1; lastOCRSeconds = result.seconds
                lastOCRAt = ISO8601DateFormatter().string(from: Date()); recentOCR.append(result.seconds)
                if recentOCR.count > 100 { recentOCR.removeFirst() }
                ocrCache.store(result.text, for: frame, signature: ocrSignature)
                text = result.text
            }
            lastError = nil
            lastProgressAt = ISO8601DateFormatter().string(from: Date())
            guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { emptyResults += 1; skipReason = "empty_text"; return }
            let event = CaptureEvent(deviceID: deviceID, app: frame.app, title: frame.title, text: text,
                method: "paddleocr", capturedAt: frame.date, trigger: "timer", phase: "snapshot", windowID: frame.window,
                resourceURI: desktopPet.resourceCapture && resource != nil && resource == browserResource(pid: context.pid, bundle: context.app) ? resource : nil)
            do { try await spool.append(event) }
            catch { queueFull = true; lastError = "spool_full"; invalidate(); return }
            lastCaptureAt = event.captured_at; lastMethod = "paddleocr"; captureCount += 1
        } catch is CancellationError { return }
        catch { if stamp == generation {
            if stage == "screenshot" { screenshotFailures += 1; lastError = "screenshot_failed" }
            else { ocrFailures += 1; lastError = "ocr_unavailable" }
        } }
    }
    private func processNext() async {
        guard !processing, let group = buffer.begin() else { return }
        processing = true; let stamp = generation
        defer { buffer.finished(); processing = false; stage = "idle"; writeStatus() }
        var recognized: [(String, WindowFrame, String)] = []
        var failed = false
        for (phase, optionalFrame) in [("before", group.before), ("after", group.after)] {
            guard let frame = optionalFrame, enabled, generation == stamp, !Task.isCancelled else { continue }
            do {
                var text = ocrCache.text(for: frame, signature: ocrSignature)
                if text == nil {
                    let delay = max(1, config.ocrMinInterval ?? 1) - (uptime - lastOCRStart)
                    if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
                    guard enabled, generation == stamp, !Task.isCancelled else { return }
                    lastOCRStart = uptime
                    stage = "ocr"; ocrAttempts += 1
                    let result = try await ocrClient.recognize(png: frame.png)
                    guard enabled, generation == stamp, !Task.isCancelled else { return }
                    text = result.text; ocrCount += 1; lastOCRSeconds = result.seconds
                    lastOCRAt = ISO8601DateFormatter().string(from: Date()); recentOCR.append(result.seconds)
                    if recentOCR.count > 100 { recentOCR.removeFirst() }
                    ocrCache.store(result.text, for: frame, signature: ocrSignature)
                }
                lastProgressAt = ISO8601DateFormatter().string(from: Date())
                guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { emptyResults += 1; skipReason = "empty_text"; continue }
                recognized.append((phase, frame, text))
            } catch is CancellationError { return }
            catch { if generation == stamp { failed = true; ocrFailures += 1; lastError = "ocr_unavailable" } }
        }
        guard enabled, generation == stamp, !Task.isCancelled else { return }
        let hasBefore = recognized.contains { $0.0 == "before" }
        let hasAfter = recognized.contains { $0.0 == "after" }
        for (phase, frame, text) in recognized {
            guard enabled, generation == stamp, !Task.isCancelled else { return }
            do {
                let event = CaptureEvent(deviceID: deviceID, app: frame.app, title: frame.title, text: text, method: "paddleocr",
                    capturedAt: frame.date, interactionID: group.id.uuidString.lowercased(), trigger: group.trigger,
                    phase: group.supplement ? "supplement" : phase, windowID: frame.window, eventAt: group.eventDate,
                    missingBefore: !hasBefore && !group.supplement, missingAfter: !hasAfter,
                    mergedCount: group.merged, droppedGroups: buffer.dropped)
                try await spool.append(event)
                lastCaptureAt = event.captured_at; lastMethod = "paddleocr"; captureCount += 1
            } catch { queueFull = true; lastError = "spool_full"; invalidate(); return }
        }
        if !failed { lastError = inputReady || !interaction ? nil : "input_monitoring_permission_required" }
    }
    private func captureAXOnly(kind: String) async {
        guard !processing, enabled, let context, uptime - lastOCRStart >= max(1, config.ocrMinInterval ?? 1) else { return }
        processing = true; let stamp = generation; lastOCRStart = uptime
        defer { processing = false }
        let result = await Task.detached(priority: .utility) { captureAX(pid: context.pid) }.value
        lastProgressAt = ISO8601DateFormatter().string(from: Date())
        guard enabled, stamp == generation, !result.secure, !excluded(result.title), !result.text.isEmpty,
              NSWorkspace.shared.frontmostApplication?.processIdentifier == context.pid else { return }
        do {
            let event = CaptureEvent(deviceID: deviceID, app: context.app, title: result.title, text: result.text, method: "ax",
                trigger: kind, phase: "snapshot", windowID: "ax:\(context.pid):\(context.title.prefix(150))")
            try await spool.append(event); captureCount += 1; lastCaptureAt = event.captured_at; lastMethod = "ax"
        } catch { lastError = "spool_full"; queueFull = true; invalidate() }
    }
    private func upload() async {
        guard !uploading else { return }; uploading = true
        defer { uploading = false }
        do {
            spooled = try await spool.files().count
            for _ in 0..<20 {
                guard let (file, data) = try await spool.next() else { spooled = 0; return }
                var request = URLRequest(url: URL(string: config.endpoint + "/api/ingest")!)
                request.httpMethod = "POST"; request.httpBody = data; request.timeoutInterval = 8
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.setValue("Bearer " + config.token, forHTTPHeaderField: "Authorization")
                let (body, response) = try await URLSession.shared.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { uploadFailures += 1; if uploadFailureSince == nil { uploadFailureSince = ISO8601DateFormatter().string(from: Date()) }; return }
                let result = try JSONSerialization.jsonObject(with: body) as? [String: [String]]
                try await spool.acknowledge(file: file, ids: Set((result?["accepted"] ?? []) + (result?["skipped"] ?? [])))
                let acknowledged = Set((result?["accepted"] ?? []) + (result?["skipped"] ?? []))
                uploadAccepted += result?["accepted"]?.count ?? 0; uploadFiltered += result?["skipped"]?.count ?? 0
                lastUploadAt = ISO8601DateFormatter().string(from: Date()); uploadFailureSince = nil
                if !acknowledged.isEmpty { queueFull = false; if lastError == "spool_full" { lastError = nil } }
                spooled = try await spool.files().count
            }
        } catch { uploadFailures += 1; if uploadFailureSince == nil { uploadFailureSince = ISO8601DateFormatter().string(from: Date()) }; queueState.title = "服务离线，文本保留在本地" }
    }
    private func writeStatus() {
        guard let path = config?.statusPath else { return }
        let active = enabled && context != nil && AXIsProcessTrusted() && (!usesOCR || (CGPreflightScreenCaptureAccess() && (!interaction || stream != nil)))
        toggle.title = desiredRunning ? "暂停采集" : "开始采集"
        state.title = !desiredRunning ? "已暂停 · 只保存文本" : locked ? "屏幕已锁定 · 采集挂起" : active ? (interaction ? "采集中 · 操作前后与页面变化" : "采集中 · 每 \(Int(snapshotSchedule.interval)) 秒一次") : "等待权限或可采集窗口"
        if ProcessInfo.processInfo.thermalState == .critical { state.title = "设备温度过高 · 采集暂挂，降温后恢复" }
        if lastError == "spool_full" { state.title = "本地队列已满，恢复服务后再开始采集" }
        queueState.title = "待识别：\(buffer.pending.count) 组 · 待上传：\(spooled) 批"
        ocrState.title = !usesOCR ? "AX 手动文字模式" : lastError == "ocr_unavailable" ? "OCR 不可用 · 请在管理菜单诊断" : "PaddleOCR · 最近 \(lastOCRSeconds.map { String(format: "%.2f 秒", $0) } ?? "尚未识别")"
        item.button?.image = NSImage(systemSymbolName: lastError == nil ? "leaf" : "exclamationmark.triangle", accessibilityDescription: lastError == nil ? "拾忆采集" : "采集需要检查")
        var status: [String: Any] = [
            "telemetry_version": 2, "process_id": processID, "stage": stage,
            "sample_attempts": snapshotSchedule.attempts, "busy_skips": snapshotSchedule.busySkips,
            "screenshots": screenshots, "screenshot_failures": screenshotFailures,
            "ocr_attempts": ocrAttempts, "ocr_successes": ocrCount, "empty_results": emptyResults,
            "upload_failures": uploadFailures, "upload_accepted": uploadAccepted, "upload_filtered": uploadFiltered,
            "last_progress_at": lastProgressAt,

            "pid": ProcessInfo.processInfo.processIdentifier, "updated_at": ISO8601DateFormatter().string(from: Date()),
            "paused": !desiredRunning, "capture_active": active, "screen_locked": locked,
            "accessibility_granted": AXIsProcessTrusted(), "screen_recording_granted": CGPreflightScreenCaptureAccess(),
            "input_monitoring_granted": inputReady || CGPreflightListenEventAccess(),
            "ocr_engine": "paddleocr", "ocr_mode": config.ocrMode ?? "always", "capture_mode": config.captureMode ?? "timer",
            "interval_seconds": snapshotSchedule.interval,
            "resource_policy": ProcessInfo.processInfo.isLowPowerModeEnabled ? "saving" : (desktopPet?.resourcePolicy ?? "standard"),
            "system_low_power": ProcessInfo.processInfo.isLowPowerModeEnabled,
            "desktop_pet": desktopPet?.diagnostics ?? [:],
            "thermal_state": [ProcessInfo.ThermalState.nominal: "nominal", .fair: "fair", .serious: "serious", .critical: "critical"][ProcessInfo.processInfo.thermalState] ?? "unknown",
            "captures": captureCount, "ocr_cache_hits": ocrCache.hits, "ocr_cache_misses": ocrCache.misses, "ocr_requests": ocrCount, "ocr_failures": ocrFailures, "pending_groups": buffer.pending.count,
            "merged_groups": buffer.merged, "dropped_groups": buffer.dropped, "missing_before": buffer.missingBefore,
            "image_bytes": buffer.bytes + snapshotImageBytes, "spooled_batches": spooled, "recent_ocr_seconds": recentOCR,
        ]
        status["last_screenshot_at"] = lastScreenshotAt; status["last_upload_at"] = lastUploadAt
        status["upload_failure_since"] = uploadFailureSince
        status["skip_reason"] = skipReason; status["queue_full"] = queueFull
        status["last_capture_at"] = lastCaptureAt; status["last_ocr_at"] = lastOCRAt
        status["last_ocr_seconds"] = lastOCRSeconds; status["last_method"] = lastMethod; status["last_error"] = lastError
        if let data = try? JSONSerialization.data(withJSONObject: status, options: [.sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        }
    }
}
MainActor.assumeIsolated {
    let app = NSApplication.shared; let delegate = AppDelegate()
    app.delegate = delegate; app.setActivationPolicy(.accessory)
    withExtendedLifetime(delegate) { app.run() }
}
