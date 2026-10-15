import AppKit
import WebKit
import WorkMemoryCore
import ImageIO

private final class PetPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}
private final class ChatPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

@MainActor
final class DesktopPet: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private let endpoint: URL
    private let token: String
    private let library: URL
    private let positionFile: URL
    private let pet = PetPanel(contentRect: NSRect(x: 50, y: 100, width: 112, height: 112), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    private var chat: ChatPanel?
    private var web: WKWebView?
    private var webChatReady = false
    private var polling: Task<Void, Never>?
    private var preferences: [String: Any] = [:]
    private var suspended = false
    private var fullscreen = false
    private var companionTrigger = "timer"
    private var companionBubble: PetPanel?
    private var companionID: String?
    private var companionTimer: Task<Void, Never>?
    private var companionToOpen: String?
    private var seenCompanions = Set<String>()
    private var attachingCompanion = false
    private var reminderBubble: PetPanel?
    private var bubbleItems: [[String: Any]] = []
    private var displayedReminders = Set<String>()
    private var reminderToOpen: [String: Any]?
    private var openAllReminders = false
    private var bubbleError: NSTextField?
    private var selectedSkin = ""
    private var skinItems: [[String: Any]] = []
    private var skinLoadFailed = false
    // Input drafts live only in this process, never in capture files or PostgreSQL.
    private var chatSession: [String: Any] = [:]
    private var lastRunState = "idle"
    private lazy var drawing = CatView()
    var diagnostics: [String: Any] {
        ["visible": pet.isVisible, "chat_open": chat?.isVisible ?? false,
         "web_loaded": web?.url != nil && web?.isLoading == false,
         "web_chat_ready": webChatReady, "web_retained": web != nil, "skin_load_failed": skinLoadFailed, "reminder_bubble_visible": reminderBubble?.isVisible ?? false,
         "reminder_bubble_count": bubbleItems.count,
         "companion_bubble_visible": companionBubble?.isVisible ?? false,
         "render_count": drawing.renderCount, "suspended": suspended,
         "skin": selectedSkin, "state": drawing.state]
    }
    var resourcePolicy = "standard"
    var resourceCapture = false

    init(endpoint: String, token: String, dataDirectory: URL) {
        self.endpoint = URL(string: endpoint)!
        self.token = token
        // The library may not exist until the first upload. Do not let URL infer
        // and cache a non-directory identity that later fails containment checks.
        library = dataDirectory.appendingPathComponent("desktop-pets", isDirectory: true)
        positionFile = dataDirectory.appendingPathComponent("pet-position.json")
        super.init()
        pet.title = "拾忆桌宠"; pet.isOpaque = false; pet.backgroundColor = .clear
        pet.hasShadow = false; pet.level = .floating; pet.hidesOnDeactivate = false
        pet.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        pet.contentView = drawing
        drawing.clicked = { [weak self] in self?.toggleChat() }
        drawing.moved = { [weak self] in self?.savePosition() }
        drawing.contextMenu = { [weak self] in self?.menu() }
        if let data = try? Data(contentsOf: positionFile), let pos = try? JSONDecoder().decode([Double].self, from: data), pos.count == 2 {
            pet.setFrameOrigin(NSPoint(x: pos[0], y: pos[1]))
        }
        clampPosition()
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(self, selector: #selector(suspend), name: NSWorkspace.willSleepNotification, object: nil)
        center.addObserver(self, selector: #selector(resume), name: NSWorkspace.didWakeNotification, object: nil)
        center.addObserver(self, selector: #selector(suspend), name: NSWorkspace.sessionDidResignActiveNotification, object: nil)
        center.addObserver(self, selector: #selector(resume), name: NSWorkspace.sessionDidBecomeActiveNotification, object: nil)
        center.addObserver(self, selector: #selector(appChanged), name: NSWorkspace.didActivateApplicationNotification, object: nil)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(suspend), name: NSNotification.Name("com.apple.screenIsLocked"), object: nil)
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(resume), name: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        startPolling()
    }
    private func startPolling() {
        polling?.cancel()
        polling = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                if !self.suspended { self.appChanged(); await self.refresh() }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }
    private func request(_ path: String, body: [String: Any]? = nil, method: String = "PATCH") async throws -> Any {
        let parts = path.split(separator: "?", maxSplits: 1).map(String.init)
        var components = URLComponents(url: endpoint.appendingPathComponent("api").appendingPathComponent(parts[0]), resolvingAgainstBaseURL: false)!
        if parts.count == 2 { components.percentEncodedQuery = parts[1] }
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 5
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        if let body {
            request.httpMethod = method; request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONSerialization.jsonObject(with: data)
    }
    private func refresh() async {
        do {
            guard let value = try await request("client/state") as? [String: Any] else { return }
            preferences = value
            resourcePolicy = value["resource_policy"] as? String ?? "standard"
            resourceCapture = value["resource_capture"] as? Bool ?? false
            let size = CGFloat(value["pet_size"] as? Int ?? 112)
            if pet.frame.width != size { pet.setContentSize(NSSize(width: size, height: size)); clampPosition() }
            let skin = value["pet_skin"] as? String ?? "builtin-cat"
            if skin != selectedSkin || skinLoadFailed { loadSkin(skin) }
            drawing.motionEnabled = value["pet_motion_enabled"] as? Bool ?? true
            if let catalog = try? await request("pet/skins") as? [String: Any] { skinItems = catalog["items"] as? [[String: Any]] ?? [] }
            updateVisibility()
            let status = value["agent_status"] as? String ?? "idle"
            drawing.toolTip = skinLoadFailed ? "形象文件丢失或损坏，已显示默认猫咪；可重新导入" : "点击打开或关闭聊天，右键更换形象"
            lastRunState = ["queued", "running"].contains(status) ? "thinking" : status == "failed" ? "error" : "idle"
            drawing.state = (value["pending_operations"] as? Int ?? 0) > 0 ? "waiting" : lastRunState
            if value["reminders_enabled"] as? Bool == true,
               let reminders = try await request("reminders?pet=true") as? [[String: Any]] {
                let live = Set(reminders.compactMap { $0["id"] as? String })
                if bubbleItems.contains(where: { !live.contains($0["id"] as? String ?? "") }) { closeBubble() }
                let unseen = reminders.filter {
                    guard let id = $0["id"] as? String, let revision = $0["revision"] as? Int else { return false }
                    return ($0["needs_display"] as? Bool == true) && !displayedReminders.contains("\(id):\(revision)")
                }
                if !unseen.isEmpty && pet.isVisible && !suspended {
                    showReminderBubble(unseen)
                    for item in unseen {
                        if let id = item["id"] as? String, let revision = item["revision"] as? Int {
                            displayedReminders.insert("\(id):\(revision)")
                            _ = try? await request("reminders/\(id)/action", body: ["action": "shown", "expected_revision": revision], method: "POST")
                        }
                    }
                }
            } else { closeBubble() }
            await refreshCompanion()
        } catch {
            closeCompanion()
            drawing.state = "offline"; drawing.toolTip = "拾忆服务尚未就绪；点击查看连接状态"
            updateVisibility()
        }
    }
    private func setPreference(_ key: String, _ value: Any) {
        Task {
            do {
                if let result = try await request("client/state", body: ["expected_revision": preferences["revision"] ?? 0, key: value]) as? [String: Any] { preferences = result }
                await refresh()
            } catch {
                await refresh()
                let alert = NSAlert(); alert.messageText = "设置未保存"; alert.informativeText = "设置可能已在网页更新，或服务暂不可用。请重新选择。"; alert.runModal()
            }
        }
    }
    @objc func toggleVisibility() { setPreference("pet_visible", !(preferences["pet_visible"] as? Bool ?? true)) }
    private func updateVisibility() {
        let shown = preferences["pet_visible"] as? Bool ?? true
        let hidden = suspended || !shown || (fullscreen && (preferences["hide_fullscreen"] as? Bool ?? true))
        drawing.animating = !hidden
        if hidden { pet.orderOut(nil); reminderBubble?.orderOut(nil); closeCompanion() } else { pet.orderFrontRegardless(); if reminderBubble != nil { positionBubble(); reminderBubble?.orderFrontRegardless() } }
    }
    @objc private func suspend() { closeCompanion(); suspended = true; drawing.animating = false; pet.orderOut(nil); reminderBubble?.orderOut(nil); chat?.orderOut(nil) }
    @objc private func resume() { suspended = false; Task { await refresh() } }
    @objc private func screensChanged() { clampPosition(); appChanged() }
    @objc private func appChanged() {
        // Geometry-only check: no screenshot, window title, or additional capture permission.
        fullscreen = false
        if let app = NSWorkspace.shared.frontmostApplication, app.processIdentifier != ProcessInfo.processInfo.processIdentifier,
           let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
            for entry in windows where (entry[kCGWindowOwnerPID as String] as? Int32) == app.processIdentifier && (entry[kCGWindowLayer as String] as? Int) == 0 {
                if let bounds = entry[kCGWindowBounds as String] as? NSDictionary,
                   let rect = CGRect(dictionaryRepresentation: bounds),
                   NSScreen.screens.contains(where: { abs(rect.width - $0.frame.width) < 2 && abs(rect.height - $0.frame.height) < 2 }) { fullscreen = true }
            }
        }
        updateVisibility()
        if fullscreen { closeCompanion() }
    }
    private func clampPosition() {
        let frame = pet.frame
        let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(frame) }) ?? NSScreen.main
        guard let area = screen?.visibleFrame else { return }
        var x = max(area.minX, min(frame.minX, area.maxX - frame.width))
        let y = max(area.minY, min(frame.minY, area.maxY - frame.height))
        if x - area.minX < 16 { x = area.minX }
        if area.maxX - frame.maxX < 16 { x = area.maxX - frame.width }
        pet.setFrameOrigin(NSPoint(x: x, y: y))
    }
    private func savePosition() {
        clampPosition(); positionBubble(); positionCompanion()
        if let data = try? JSONEncoder().encode([pet.frame.minX, pet.frame.minY]) { try? data.write(to: positionFile, options: .atomic) }
    }
    private func positionBubble() {
        guard let bubble = reminderBubble else { return }
        let area = pet.screen?.visibleFrame ?? NSScreen.main!.visibleFrame
        let x = min(max(area.minX + 8, pet.frame.midX - bubble.frame.width / 2), area.maxX - bubble.frame.width - 8)
        let above = pet.frame.maxY + 10
        let y = above + bubble.frame.height <= area.maxY ? above : max(area.minY + 8, pet.frame.minY - bubble.frame.height - 10)
        bubble.setFrameOrigin(NSPoint(x: x, y: y))
    }
    @objc private func closeBubble() {
        reminderBubble?.orderOut(nil); reminderBubble?.contentView = nil; reminderBubble = nil
        bubbleItems = []; bubbleError = nil
    }
    private func showReminderBubble(_ items: [[String: Any]]) {
        closeCompanion(); closeBubble(); bubbleItems = items
        let panel = PetPanel(contentRect: NSRect(x: 0, y: 0, width: 330, height: 195), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "拾忆 · 到点提醒"
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.level = .floating; panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        let background = NSVisualEffectView(); background.material = .popover; background.blendingMode = .behindWindow
        background.state = .active; background.wantsLayer = true; background.layer?.cornerRadius = 18
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        let heading = NSTextField(labelWithString: items.count > 1 ? "小猫：有 \(items.count) 件事到时间啦" : "小猫来提醒你啦")
        heading.font = .systemFont(ofSize: 14, weight: .semibold)
        let body = NSTextField(wrappingLabelWithString: items.count > 1 ? items.prefix(3).compactMap { $0["text"] as? String }.joined(separator: "\n") : items.first?["text"] as? String ?? "到时间啦")
        body.font = .systemFont(ofSize: 13); body.maximumNumberOfLines = 4; body.lineBreakMode = .byTruncatingTail
        let controls = NSStackView(); controls.orientation = .horizontal; controls.spacing = 6
        func button(_ title: String, _ selector: Selector) -> NSButton { let b = NSButton(title: title, target: self, action: selector); b.bezelStyle = .rounded; b.font = .systemFont(ofSize: 11); return b }
        controls.addArrangedSubview(button(items.count > 1 ? "查看全部" : "打开对话", #selector(openReminderChat)))
        if items.count == 1 {
            controls.addArrangedSubview(button("完成", #selector(completeReminder)))
            controls.addArrangedSubview(button("10分钟后", #selector(snoozeReminder)))
        }
        controls.addArrangedSubview(button("关闭", #selector(closeBubble)))
        let error = NSTextField(wrappingLabelWithString: "不抢输入焦点 · 关闭后仍可在提醒中查看")
        error.font = .systemFont(ofSize: 10); error.textColor = .secondaryLabelColor; bubbleError = error
        for view in [heading, body, controls, error] { stack.addArrangedSubview(view) }
        background.addSubview(stack); panel.contentView = background
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 16), stack.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -16), stack.topAnchor.constraint(equalTo: background.topAnchor, constant: 16), stack.bottomAnchor.constraint(lessThanOrEqualTo: background.bottomAnchor, constant: -12)])
        reminderBubble = panel; positionBubble(); panel.orderFrontRegardless(); drawing.state = "waiting"
    }
    @objc private func completeReminder() { actOnReminder("complete") }
    @objc private func snoozeReminder() { actOnReminder("snooze") }
    private func actOnReminder(_ action: String) {
        guard let item = bubbleItems.first, let id = item["id"] as? String, let revision = item["revision"] as? Int else { return }
        Task {
            do {
                _ = try await request("reminders/\(id)/action", body: ["action": action, "expected_revision": revision], method: "POST")
                closeBubble(); await refresh()
            } catch { bubbleError?.stringValue = "未能处理，可能已在另一端修改；请打开对话查看。" }
        }
    }
    @objc private func openReminderChat() {
        guard let item = bubbleItems.first else { return }
        reminderToOpen = item
        let many = bubbleItems.count > 1
        openAllReminders = many
        closeBubble(); openChat()
        if webChatReady, let web, let data = try? JSONSerialization.data(withJSONObject: item), let json = String(data: data, encoding: .utf8) {
            web.evaluateJavaScript("window.dispatchEvent(new CustomEvent('open-reminder',{detail:\(json)}));" + (many ? "window.dispatchEvent(new Event('show-reminders'));" : ""), completionHandler: nil)
            reminderToOpen = nil; openAllReminders = false
        }
    }
    @objc func toggleChat() {
        if let chat, chat.isVisible { chat.performClose(nil) } else { openChat() }
    }
    @objc func openChat() {
        closeCompanion()
        if let chat { chat.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let panel = ChatPanel(contentRect: NSRect(x: 100, y: 100, width: 420, height: 650), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        panel.title = "拾忆 · 和桌宠聊聊"; panel.level = .floating; panel.hidesOnDeactivate = false
        panel.minSize = NSSize(width: 340, height: 420); panel.delegate = self
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "petState")
        configuration.userContentController.add(self, name: "petSession")
        let quoted = String(data: try! JSONEncoder().encode(token), encoding: .utf8)!
        let origin = String(data: try! JSONEncoder().encode(endpoint.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))), encoding: .utf8)!
        configuration.userContentController.addUserScript(WKUserScript(source: "if(location.origin === \(origin)){sessionStorage.setItem('workmemory-token',\(quoted));}", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        if let data = try? JSONSerialization.data(withJSONObject: chatSession), let json = String(data: data, encoding: .utf8) {
            configuration.userContentController.addUserScript(WKUserScript(source: "if(location.origin === \(origin)){window.__petChatSession = \(json);}", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = self; view.uiDelegate = self; panel.contentView = view
        var url = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)!
        url.queryItems = [URLQueryItem(name: "surface", value: "pet")]
        if let id = reminderToOpen?["id"] as? String { url.queryItems?.append(URLQueryItem(name: "reminder", value: id)) }
        if let conversation = companionToOpen { url.queryItems?.append(URLQueryItem(name: "companion_conversation", value: conversation)) }
        if openAllReminders { url.queryItems?.append(URLQueryItem(name: "reminders", value: "all")) }
        view.load(URLRequest(url: url.url!))
        web = view; chat = panel
        let area = pet.screen?.visibleFrame ?? NSScreen.main!.visibleFrame
        panel.setFrameOrigin(NSPoint(x: min(max(area.minX, pet.frame.minX - 300), area.maxX - 420), y: min(max(area.minY, pet.frame.minY + 100), area.maxY - 650)))
        panel.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        Task { [weak self, weak webView] in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard let self, let webView, self.web === webView else { return }
            self.webChatReady = (try? await webView.evaluateJavaScript("Boolean(document.querySelector('[aria-label=\"提问\"]'))")) as? Bool ?? false
        }
    }
    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow, closing === chat else { return }
        webChatReady = false
        companionToOpen = nil
        reminderToOpen = nil; openAllReminders = false
        web?.configuration.userContentController.removeScriptMessageHandler(forName: "petSession")
        web?.stopLoading(); web?.configuration.userContentController.removeScriptMessageHandler(forName: "petState")
        web?.navigationDelegate = nil; web?.uiDelegate = nil; chat?.contentView = nil; web = nil; chat = nil
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == endpoint.scheme && url.host == endpoint.host && url.port == endpoint.port { decisionHandler(.allow) }
        else {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard frame.isMainFrame, frame.securityOrigin.host == endpoint.host,
              frame.securityOrigin.protocol == endpoint.scheme,
              frame.securityOrigin.port == (endpoint.port ?? (endpoint.scheme == "https" ? 443 : 80)),
              let panel = chat else { completionHandler(false); return }
        let alert = NSAlert(); alert.messageText = "拾忆"; alert.informativeText = String(message.prefix(2000))
        alert.addButton(withTitle: "确认"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: panel) { result in completionHandler(result == .alertFirstButtonReturn) }
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.webView === web, message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.host == endpoint.host,
              message.frameInfo.securityOrigin.protocol == endpoint.scheme,
              message.frameInfo.securityOrigin.port == (endpoint.port ?? (endpoint.scheme == "https" ? 443 : 80)) else { return }
        if message.name == "petSession", let snapshot = message.body as? [String: Any] {
            chatSession = snapshot
            return
        }
        guard message.name == "petState", let state = message.body as? String,
              ["idle", "thinking", "searching", "waiting", "completed", "error"].contains(state) else { return }
        drawing.state = state
    }
    private func menu() -> NSMenu {
        let menu = NSMenu()
        for (title, action) in [("和拾忆聊天", #selector(openChat)), ("隐藏桌宠", #selector(toggleVisibility)), ("导入 PNG 形象…", #selector(importPNG)), ("导入动画皮肤文件夹…", #selector(importSkin)), ("恢复默认猫咪", #selector(defaultSkin)), ("重置位置", #selector(resetPosition))] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: ""); item.target = self; menu.addItem(item)
        }
        let proactive = preferences["proactive"] as? [String: Any] ?? [:]
        let companionship = NSMenuItem(title: "主动陪伴", action: #selector(toggleCompanion), keyEquivalent: "")
        companionship.target = self; companionship.state = proactive["enabled"] as? Bool == true ? .on : .off
        menu.addItem(companionship)
        let quiet = NSMenuItem(title: "安静一小时", action: #selector(muteCompanion), keyEquivalent: "")
        quiet.target = self; menu.addItem(quiet)
        let today = NSMenuItem(title: "今天先安静", action: #selector(muteToday), keyEquivalent: "")
        today.target = self; menu.addItem(today)
        let motion = NSMenuItem(title: "单图形象轻微动效", action: #selector(toggleMotion), keyEquivalent: "")
        motion.target = self; motion.state = drawing.motionEnabled ? .on : .off; menu.addItem(motion)
        for skin in skinItems {
            guard let id = skin["id"] as? String, let name = skin["name"] as? String else { continue }
            let item = NSMenuItem(title: "形象：" + name, action: #selector(selectSkin(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = id; item.state = id == selectedSkin ? .on : .off; menu.addItem(item)
        }
        return menu
    }
    private var companionAvailable: Bool {
        let config = preferences["proactive"] as? [String: Any] ?? [:]
        return config["enabled"] as? Bool == true && pet.isVisible && !suspended && !fullscreen
            && chat?.isVisible != true && reminderBubble == nil && lastRunState != "thinking"
            && resourcePolicy != "saving" && !ProcessInfo.processInfo.isLowPowerModeEnabled && !attachingCompanion
    }
    private func refreshCompanion() async {
        let available = companionAvailable
        if !available { closeCompanion() }
        do {
            _ = try await request("client/proactive/presence", body: ["available": available], method: "POST")
            guard available, companionAvailable else { return }
            let result = try await request("client/proactive") as? [String: Any]
            guard let item = result?["item"] as? [String: Any], let id = item["id"] as? String,
                  let text = item["text"] as? String, companionAvailable else { closeCompanion(); return }
            if companionID == id { return }
            guard item["status"] as? String == "ready", !seenCompanions.contains(id) else { return }
            _ = try await request("client/proactive/\(id)/action", body: ["action": "shown"], method: "POST")
            guard companionAvailable else { return }
            seenCompanions.insert(id)
            if seenCompanions.count > 100 { seenCompanions = [id] }
            companionTrigger = item["trigger"] as? String ?? "timer"
            showCompanion(id: id, text: text)
        } catch { closeCompanion() }
    }
    private func positionCompanion() {
        guard let panel = companionBubble, let area = pet.screen?.visibleFrame ?? NSScreen.main?.visibleFrame else { return }
        let x = min(max(area.minX + 8, pet.frame.midX - panel.frame.width / 2), area.maxX - panel.frame.width - 8)
        let above = pet.frame.maxY + 10
        let y = above + panel.frame.height <= area.maxY ? above : max(area.minY + 8, pet.frame.minY - panel.frame.height - 10)
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }
    private func showCompanion(id: String, text: String) {
        closeCompanion()
        companionID = id
        let textHeight = (text as NSString).boundingRect(with: NSSize(width: 230, height: 1000), options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [.font: NSFont.systemFont(ofSize: 14)]).height
        let panel = PetPanel(contentRect: NSRect(x: 0, y: 0, width: 280, height: max(72, ceil(textHeight) + 34)), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "拾忆 · 陪你一会儿"; panel.isOpaque = false; panel.backgroundColor = .clear
        panel.hasShadow = true; panel.level = .floating; panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .ignoresCycle]
        let background = NSVisualEffectView(); background.material = .popover; background.blendingMode = .behindWindow
        background.state = .active; background.wantsLayer = true; background.layer?.cornerRadius = 18
        let speech = NSButton(title: text, target: self, action: #selector(openCompanionChat))
        speech.isBordered = false; speech.font = .systemFont(ofSize: 14); speech.alignment = .left
        speech.cell?.wraps = true; speech.cell?.lineBreakMode = .byWordWrapping
        speech.toolTip = "点击接着聊"; speech.setAccessibilityLabel(text + "，点击接着聊")
        speech.translatesAutoresizingMaskIntoConstraints = false
        let close = NSButton(title: "×", target: self, action: #selector(dismissCompanion))
        close.isBordered = false; close.setAccessibilityLabel("关闭陪伴气泡"); close.translatesAutoresizingMaskIntoConstraints = false
        let more = NSButton(title: "⋯", target: self, action: #selector(companionMenu(_:)))
        more.isBordered = false; more.setAccessibilityLabel("陪伴反馈"); more.translatesAutoresizingMaskIntoConstraints = false
        background.addSubview(more)
        NSLayoutConstraint.activate([more.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -5), more.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -4)])
        background.addSubview(speech); background.addSubview(close)
        NSLayoutConstraint.activate([
            speech.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 16),
            speech.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -30),
            speech.topAnchor.constraint(equalTo: background.topAnchor, constant: 12),
            speech.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -12),
            close.topAnchor.constraint(equalTo: background.topAnchor, constant: 4),
            close.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -6),
        ])
        panel.contentView = background; companionBubble = panel; positionCompanion(); panel.orderFrontRegardless()
        companionTimer = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard !Task.isCancelled else { return }
            self?.dismissCompanion()
        }
    }
    @objc private func companionMenu(_ sender: NSButton) {
        let menu = NSMenu()
        if companionTrigger == "timer" {
            for (title, choice) in [("喜欢这类话", "more"), ("这个话题少说", "less"), ("恢复默认偏好", "normal")] {
                let item = NSMenuItem(title: title, action: #selector(companionFeedback(_:)), keyEquivalent: "")
                item.target = self; item.representedObject = ["id": companionID ?? "", "preference": choice]; menu.addItem(item)
            }
        }
        let quiet = NSMenuItem(title: "今天先安静", action: #selector(muteToday), keyEquivalent: "")
        quiet.target = self; menu.addItem(quiet)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: sender.bounds.height), in: sender)
    }
    @objc private func companionFeedback(_ sender: NSMenuItem) {
        guard let value = sender.representedObject as? [String: String], let id = value["id"], let preference = value["preference"], let revision = preferences["revision"] as? Int else { return }
        Task {
            do {
                _ = try await request("client/proactive/\(id)/feedback", body: ["preference": preference, "expected_revision": revision], method: "POST")
                closeCompanion(); await refresh()
            } catch { drawing.state = "error" }
        }
    }
    @objc private func muteToday() {
        Task {
            do { _ = try await request("client/proactive/mute", body: ["scope": "today"], method: "POST"); closeCompanion(); await refresh() }
            catch { drawing.state = "error" }
        }
    }
    private func closeCompanion() {
        companionTimer?.cancel(); companionTimer = nil
        companionBubble?.orderOut(nil); companionBubble?.contentView = nil; companionBubble = nil; companionID = nil
    }
    @objc private func dismissCompanion() {
        let id = companionID
        closeCompanion()
        if let id { Task { _ = try? await request("client/proactive/\(id)/action", body: ["action": "close"], method: "POST") } }
    }
    @objc private func toggleCompanion() {
        var config = preferences["proactive"] as? [String: Any] ?? [:]
        config["enabled"] = !(config["enabled"] as? Bool ?? false)
        closeCompanion(); setPreference("proactive", config)
    }
    @objc private func muteCompanion() {
        closeCompanion()
        Task { _ = try? await request("client/proactive/mute", body: [:], method: "POST"); await refresh() }
    }
    @objc private func openCompanionChat() {
        guard let id = companionID, !attachingCompanion else { return }
        attachingCompanion = true; companionTimer?.cancel()
        Task {
            defer { attachingCompanion = false }
            var body: [String: Any] = ["action": "attach"]
            if let selected = chatSession["selected"] as? String, !selected.isEmpty { body["conversation_id"] = selected }
            do {
                guard let result = try await request("client/proactive/\(id)/action", body: body, method: "POST") as? [String: Any],
                      let conversation = result["conversation_id"] as? String else { closeCompanion(); return }
                companionToOpen = conversation
                closeCompanion(); openChat()
                if webChatReady, let web, let data = try? JSONSerialization.data(withJSONObject: result), let json = String(data: data, encoding: .utf8) {
                    _ = try? await web.evaluateJavaScript("window.dispatchEvent(new CustomEvent('open-companion',{detail:\(json)}));")
                    companionToOpen = nil
                }
            } catch {
                closeCompanion()
                drawing.toolTip = "这句陪伴已过时；点击桌宠仍可聊天"
            }
        }
    }
    @objc private func toggleMotion() { setPreference("pet_motion_enabled", !drawing.motionEnabled) }
    @objc private func selectSkin(_ sender: NSMenuItem) { if let id = sender.representedObject as? String { setPreference("pet_skin", id) } }
    @objc private func defaultSkin() { setPreference("pet_skin", "builtin-cat") }
    @objc private func resetPosition() { pet.setFrameOrigin(NSScreen.main?.visibleFrame.origin ?? .zero); savePosition() }
    private func importError(_ message: String) {
        let alert = NSAlert(); alert.messageText = "无法导入形象"; alert.informativeText = message; alert.runModal()
    }
    @objc private func importPNG() {
        let picker = NSOpenPanel(); picker.canChooseDirectories = false; picker.canChooseFiles = true; picker.allowsMultipleSelection = false
        picker.allowedContentTypes = [.png]; picker.prompt = "预览形象"
        guard picker.runModal() == .OK, let file = picker.url else { return }
        do {
            guard let size = try file.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 32 * 1024 * 1024 else { throw CocoaError(.fileReadTooLarge) }
            let bytes = try Data(contentsOf: file)
            guard let source = CGImageSourceCreateWithData(bytes as CFData, nil), CGImageSourceGetCount(source) == 1,
                  CGImageSourceGetType(source) as String? == "public.png",
                  let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
                  let width = properties[kCGImagePropertyPixelWidth] as? Int, let height = properties[kCGImagePropertyPixelHeight] as? Int,
                  width > 0, height > 0, width <= 4096, height <= 4096,
                  let image = NSImage(data: bytes) else { throw CocoaError(.fileReadCorruptFile) }
            let alert = NSAlert(); alert.messageText = "使用这个形象？"; alert.informativeText = "自动去掉透明留白，按原比例显示。透明背景效果更自然；普通背景会保留。"
            let preview = NSImageView(frame: NSRect(x: 0, y: 0, width: 220, height: 180)); preview.image = image; preview.imageScaling = .scaleProportionallyUpOrDown
            alert.accessoryView = preview; alert.addButton(withTitle: "应用此形象"); alert.addButton(withTitle: "取消")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            uploadSkin(bytes, name: file.deletingPathExtension().lastPathComponent)
        } catch { importError("请选择完整的静态 PNG，文件不超过 32 MiB、宽高不超过 4096 像素。") }
    }
    @objc private func importSkin() {
        let picker = NSOpenPanel(); picker.canChooseDirectories = true; picker.canChooseFiles = false; picker.allowsMultipleSelection = false
        picker.prompt = "导入皮肤"; picker.message = "选择包含 pet.json 和 PNG 图集的文件夹"
        guard picker.runModal() == .OK, let folder = picker.url else { return }
        do {
            let (skin, image) = try PetSkin.load(folder: folder)
            let data = try JSONEncoder().encode(skin)
            uploadSkin(try Data(contentsOf: image), name: skin.name, manifest: String(data: data, encoding: .utf8))
        } catch { importError("请检查版本、图片尺寸、动画帧和文件路径。皮肤只能包含本地图片与描述数据。") }
    }
    private func uploadSkin(_ image: Data, name: String, manifest: String? = nil) {
        Task {
            do {
                let boundary = UUID().uuidString
                var data = Data()
                func field(_ key: String, _ value: String) {
                    data.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n".utf8))
                }
                field("name", name)
                if let manifest { field("manifest", manifest) }
                data.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"image\"; filename=\"image.png\"\r\nContent-Type: image/png\r\n\r\n".utf8))
                data.append(image); data.append(Data("\r\n--\(boundary)--\r\n".utf8))
                var request = URLRequest(url: endpoint.appendingPathComponent("api/pet/skins"))
                request.httpMethod = "POST"; request.httpBody = data; request.timeoutInterval = 60
                request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
                request.setValue("multipart/form-data; boundary=" + boundary, forHTTPHeaderField: "Content-Type")
                let (body, response) = try await URLSession.shared.data(for: request)
                let value = try JSONSerialization.jsonObject(with: body) as? [String: Any]
                guard (response as? HTTPURLResponse)?.statusCode == 201, let id = value?["id"] as? String else {
                    importError(value?["detail"] as? String ?? "导入失败，请检查文件后重试。")
                    return
                }
                await refresh()
                setPreference("pet_skin", id)
            } catch { importError("拾忆服务暂不可用，形象尚未应用。请启动服务后重试。") }
        }
    }
    private func loadSkin(_ key: String) {
        selectedSkin = key; skinLoadFailed = false
        if key == "builtin-cat" { drawing.skin = nil; drawing.atlas = nil; return }
        let folder = library.appendingPathComponent(key, isDirectory: true)
        guard key.count == 64, key.allSatisfy({ $0.isHexDigit }),
              folder.resolvingSymlinksInPath().deletingLastPathComponent() == library.resolvingSymlinksInPath(),
              let (skin, image) = try? PetSkin.load(folder: folder), let bitmap = NSImage(contentsOf: image), bitmap.isValid else {
            skinLoadFailed = true; drawing.skin = nil; drawing.atlas = nil; return
        }
        drawing.skin = skin; drawing.atlas = bitmap
    }

}

@MainActor
private final class CatView: NSView {
    var clicked: (() -> Void)?
    var moved: (() -> Void)?
    var contextMenu: (() -> NSMenu?)?
    var state = "idle" { didSet { if state != oldValue { needsDisplay = true } } }
    var skin: PetSkin? { didSet { schedule(); needsDisplay = true } }
    var atlas: NSImage? { didSet { needsDisplay = true } }
    var motionEnabled = true { didSet { if motionEnabled != oldValue { schedule(); needsDisplay = true } } }
    var animating = false { didSet { if animating != oldValue { schedule() } } }
    private var timer: Timer?
    private var animationTick = 0
    private(set) var renderCount = 0
    private var anchor: NSPoint?
    private var windowAnchor: NSPoint?
    private var dragged = false
    override var isOpaque: Bool { false }
    private func schedule() {
        timer?.invalidate(); timer = nil
        guard animating && (skin?.presentation != "static" || motionEnabled) else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 12, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.animationTick += 1; self?.needsDisplay = true }
        }
    }
    override func mouseDown(with event: NSEvent) { anchor = window?.convertPoint(toScreen: event.locationInWindow); windowAnchor = window?.frame.origin; dragged = false }
    override func mouseDragged(with event: NSEvent) {
        guard let anchor, let origin = windowAnchor else { return }
        let p = window?.convertPoint(toScreen: event.locationInWindow) ?? NSEvent.mouseLocation; let dx = p.x - anchor.x; let dy = p.y - anchor.y
        if abs(dx) + abs(dy) > 6 { dragged = true; state = "walk"; window?.setFrameOrigin(NSPoint(x: origin.x + dx, y: origin.y + dy)) }
    }
    override func mouseUp(with event: NSEvent) { if dragged { moved?() } else { clicked?() }; anchor = nil; state = "idle" }
    override func rightMouseDown(with event: NSEvent) { if let menu = contextMenu?() { NSMenu.popUpContextMenu(menu, with: event, for: self) } }
    override func draw(_ dirtyRect: NSRect) {
        renderCount += 1
        if skin?.presentation == "static", let atlas {
            let area = bounds.insetBy(dx: 5, dy: 5)
            let ratio = min(area.width / atlas.size.width, area.height / atlas.size.height)
            let width = atlas.size.width * ratio, height = atlas.size.height * ratio
            let lift = motionEnabled ? sin(Double(animationTick) / 18) * 2 : 0
            let target = NSRect(x: area.midX - width / 2, y: area.midY - height / 2 + lift, width: width, height: height)
            atlas.draw(in: target, from: .zero, operation: .sourceOver, fraction: 1)
            if state != "idle" && state != "walk" {
                let color: NSColor = ["thinking", "searching"].contains(state) ? .systemBlue : state == "waiting" ? .systemOrange : state == "completed" ? .systemGreen : .systemGray
                let badge = NSBezierPath(ovalIn: NSRect(x: bounds.maxX - 15, y: bounds.minY + 4, width: 11, height: 11))
                NSColor.white.setStroke(); badge.lineWidth = 3; badge.stroke(); color.setFill(); badge.fill()
            }
            return
        }
        if let skin, let atlas, let animation = skin.animations[state] ?? skin.animations["idle"] {
            let i = animation.frames[Int(Double(animationTick) * animation.fps / 12) % animation.frames.count]
            let w = atlas.size.width / CGFloat(skin.columns), h = atlas.size.height / CGFloat(skin.rows)
            atlas.draw(in: bounds, from: NSRect(x: CGFloat(i % skin.columns) * w, y: atlas.size.height - CGFloat(i / skin.columns + 1) * h, width: w, height: h), operation: .sourceOver, fraction: 1)
            return
        }
        // Original vector cat. No third-party artwork or extra renderer required.
        let scale = bounds.width / 112
        NSGraphicsContext.saveGraphicsState()
        let transform = NSAffineTransform(); transform.scale(by: scale); transform.concat()
        let bounce = sin(Double(animationTick) / 18) * 1.4
        let body = NSBezierPath(roundedRect: NSRect(x: 26, y: 12 + bounce, width: 60, height: 55), xRadius: 26, yRadius: 26)
        NSColor(calibratedRed: 0.88, green: 0.69, blue: 0.43, alpha: 1).setFill(); body.fill()
        let tail = NSBezierPath(); tail.move(to: NSPoint(x: 80, y: 22)); tail.curve(to: NSPoint(x: 99, y: 45 + bounce), controlPoint1: NSPoint(x: 111, y: 8), controlPoint2: NSPoint(x: 106, y: 40)); tail.lineWidth = 9; tail.lineCapStyle = .round; NSColor(calibratedRed: 0.79, green: 0.57, blue: 0.32, alpha: 1).setStroke(); tail.stroke()
        let head = NSBezierPath(roundedRect: NSRect(x: 20, y: 46 + bounce, width: 72, height: 50), xRadius: 23, yRadius: 23)
        NSColor(calibratedRed: 0.96, green: 0.78, blue: 0.52, alpha: 1).setFill(); head.fill()
        for x in [CGFloat(24), CGFloat(69)] {
            let ear = NSBezierPath(); ear.move(to: NSPoint(x: x, y: 80 + bounce)); ear.line(to: NSPoint(x: x + 4, y: 108 + bounce)); ear.line(to: NSPoint(x: x + 20, y: 88 + bounce)); ear.close(); ear.fill()
        }
        NSColor(calibratedRed: 0.19, green: 0.21, blue: 0.19, alpha: 1).setFill()
        let blink = animationTick % 84 < 3
        for x in [CGFloat(40), CGFloat(68)] { NSBezierPath(ovalIn: NSRect(x: x, y: 69 + bounce, width: 5, height: blink ? 2 : 8)).fill() }
        NSColor.systemPink.setFill(); NSBezierPath(ovalIn: NSRect(x: 53, y: 62 + bounce, width: 7, height: 4)).fill()
        let color: NSColor = ["thinking", "searching"].contains(state) ? .systemBlue : state == "waiting" ? .systemOrange : ["error", "offline"].contains(state) ? .systemGray : .systemGreen
        color.setFill(); NSBezierPath(ovalIn: NSRect(x: 51, y: 35, width: 10, height: 10)).fill()
        NSGraphicsContext.restoreGraphicsState()
    }
}
