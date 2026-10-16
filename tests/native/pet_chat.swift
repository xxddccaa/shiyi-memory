// Compiled with the actual DesktopPet implementation. No capture API or persistent user data.
import AppKit
import WebKit

@main
struct PetWebCheck {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in
            var stage = "chat"
            do {
                let env = ProcessInfo.processInfo.environment
                let directory = URL(fileURLWithPath: env["WORKMEMORY_NATIVE_OUTPUT"]!)
                try String(ProcessInfo.processInfo.processIdentifier).write(to: directory.appendingPathComponent("pid.txt"), atomically: true, encoding: .utf8)
                let skinDirectory = URL(fileURLWithPath: env["WORKMEMORY_NATIVE_DATA"]!).appendingPathComponent("desktop-pets")
                guard !FileManager.default.fileExists(atPath: skinDirectory.path) else { throw CheckError.failed }
                let pet = DesktopPet(endpoint: env["WORKMEMORY_ENDPOINT"]!, token: env["WORKMEMORY_TEST_TOKEN"]!, dataDirectory: URL(fileURLWithPath: env["WORKMEMORY_NATIVE_DATA"]!))
                try await waitNative { pet.diagnostics["visible"] as? Bool == true }
                try tapPet(app)
                guard let panel = app.windows.first(where: { $0.title == "拾忆 · 和桌宠聊聊" && $0.isVisible }), let web = panel.contentView as? WKWebView else { throw CheckError.failed }
                stage = "load chat"
                try await until(web, "Boolean(document.querySelector('[aria-label=提问]'))")
                // Use the real React event path, authenticated API and Pi process.
                _ = try await web.evaluateJavaScript("""
                    (() => { const input = document.querySelector('[aria-label=提问]');
                    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '蓝鲸验收进展如何？');
                    input.dispatchEvent(new Event('input', {bubbles: true})); return true; })()
                    """)
                try await until(web, "!document.querySelector('[aria-label=发送问题]').disabled")
                _ = try await web.evaluateJavaScript("document.querySelector('[aria-label=发送问题]').click(); true")
                try await until(web, "Boolean(document.querySelector('.answer-bubble'))")
                var measurements: [[String: Any]] = []
                for (width, height) in [(340, 420), (420, 650), (560, 760)] {
                    panel.setContentSize(NSSize(width: width, height: height))
                    try await Task.sleep(nanoseconds: 200_000_000)
                    let value = try await web.evaluateJavaScript("""
                        (() => { const box = document.querySelector('.chat-composer').getBoundingClientRect();
                        const transcript = document.querySelector('.pet-transcript');
                        return {width: innerWidth, height: innerHeight,
                        noOverflow: document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight,
                        composerVisible: box.bottom <= innerHeight && box.width >= innerWidth-40,
                        transcriptHeight: transcript.clientHeight,
                        transcriptContained: transcript.scrollWidth <= transcript.clientWidth}; })()
                        """) as! [String: Any]
                    guard value["noOverflow"] as? Bool == true, value["composerVisible"] as? Bool == true,
                          (value["transcriptHeight"] as? Int ?? 0) >= 200,
                          value["transcriptContained"] as? Bool == true else { throw CheckError.failed }
                    measurements.append(value)
                    stage = "tools panel at \(width)"
                    _ = try await web.evaluateJavaScript("document.querySelector('.chat-tools button:nth-child(2)').click(); true")
                    try await until(web, "Boolean(document.querySelector('dialog[open]'))")
                    _ = try await web.evaluateJavaScript("document.querySelector('#tool-tab-coding').click(); true")
                    try await until(web, "document.querySelector('#tool-tab-coding').getAttribute('aria-selected') === 'true'")
                    _ = try await web.evaluateJavaScript("""
                        (() => { const input = document.querySelector('[aria-label=添加本机目录]');
                        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '/synthetic/native-draft');
                        input.dispatchEvent(new Event('input', {bubbles: true})); return true; })()
                        """)
                    _ = try await web.evaluateJavaScript("document.querySelector('[aria-label=关闭面板]').click(); true")
                    try await until(web, "!document.querySelector('dialog[open]')")
                    _ = try await web.evaluateJavaScript("document.querySelector('.chat-tools button:nth-child(2)').click(); true")
                    try await until(web, "document.querySelector('[aria-label=添加本机目录]')?.value === '/synthetic/native-draft'")
                    let toolImage = try await web.takeSnapshot(configuration: nil)
                    let toolBitmap = NSBitmapImageRep(data: toolImage.tiffRepresentation!)!
                    try toolBitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent("pet-tools-native-\(width).png"))
                    _ = try await web.evaluateJavaScript("document.querySelector('[aria-label=关闭面板]').click(); true")
                    try await until(web, "!document.querySelector('dialog[open]')")
                    let image = try await web.takeSnapshot(configuration: nil)
                    let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
                    try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent("pet-native-\(width).png"))
                }
                _ = try await web.evaluateJavaScript("document.querySelector('.citations button').click(); true")
                try await until(web, "Boolean(document.querySelector('dialog[open]'))")
                _ = try await web.evaluateJavaScript("document.querySelector('[aria-label=关闭来源]').click(); true")
                try await until(web, "!document.querySelector('dialog[open]')")
                stage = "draft and toggle close"
                _ = try await web.evaluateJavaScript("""
                    (() => { const input = document.querySelector('[aria-label=提问]');
                    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '尚未发送的合成草稿');
                    input.dispatchEvent(new Event('input', {bubbles: true})); return true; })()
                    """)
                try await until(web, "window.__petChatSession?.drafts[sessionStorage.getItem('workmemory-conversation')] === '尚未发送的合成草稿'")
                try tapPet(app)
                guard pet.diagnostics["chat_open"] as? Bool == false,
                      pet.diagnostics["web_retained"] as? Bool == false else { throw CheckError.failed }
                try tapPet(app)
                guard let reopened = app.windows.first(where: { $0.title == "拾忆 · 和桌宠聊聊" && $0.isVisible }), let newWeb = reopened.contentView as? WKWebView else { throw CheckError.failed }
                guard newWeb !== web else { throw CheckError.failed }
                try await until(newWeb, "Boolean(document.querySelector('.answer-bubble'))")
                try await until(newWeb, "document.querySelector('[aria-label=提问]').value === '尚未发送的合成草稿'")
                stage = "drag does not close chat"
                try tapPet(app, drag: true)
                guard pet.diagnostics["chat_open"] as? Bool == true else { throw CheckError.failed }
                stage = "first upload after native startup"
                guard !FileManager.default.fileExists(atPath: skinDirectory.path) else { throw CheckError.failed }
                stage = "upload PNG API"
                let imported = try await uploadPNG()
                guard let skinID = imported["id"] as? String else { throw CheckError.failed }
                stage = "select uploaded PNG"
                let preference = try await api("client/state")
                _ = try await api("client/state", body: ["expected_revision": preference["revision"]!, "pet_skin": skinID, "pet_motion_enabled": false], method: "PATCH")
                stage = "native selected PNG"
                try await waitNative { pet.diagnostics["skin"] as? String == skinID }
                stage = "native decoded PNG"
                guard pet.diagnostics["skin_load_failed"] as? Bool == false else { throw CheckError.failed }
                stage = "native rendered PNG"
                if let window = app.windows.first(where: { $0.title == "拾忆桌宠" }), let view = window.contentView,
                   let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("custom-pet.png"))
                    // AppKit applies the display profile; verify visible red dominance,
                    // not exact source RGB bytes. The fallback cat is gold, not red.
                    guard let center = bitmap.colorAt(x: bitmap.pixelsWide / 2, y: bitmap.pixelsHigh / 2)?.usingColorSpace(.sRGB),
                          center.alphaComponent > 0.9, center.redComponent > 0.8,
                          center.redComponent > center.greenComponent * 2,
                          center.redComponent > center.blueComponent * 2 else { throw CheckError.failed }
                } else { throw CheckError.failed }
                // Actual AppKit bubble, real durable delivery and authenticated actions.
                // Activation is a best-effort request on macOS 14+ (NSApplication.h).
                // Preserve whichever app actually owns focus; a refused activation is not
                // evidence that the reminder stole it. All preservation assertions remain.
                stage = "settle actual foreground baseline"
                try await Task.sleep(nanoseconds: 500_000_000)
                stage = "snooze bubble"
                let first = try await schedule("原生合成延后提醒")
                let keyBeforeReminder = app.keyWindow
                let foregroundBefore = NSWorkspace.shared.frontmostApplication?.processIdentifier
                let activeBefore = app.isActive
                try await waitNative { pet.diagnostics["reminder_bubble_visible"] as? Bool == true }
                stage = "bubble window and controls"
                guard let bubble = app.windows.first(where: { $0.title == "拾忆 · 到点提醒" && $0.isVisible }),
                      let snooze = button(bubble.contentView!, "10分钟后") else { throw CheckError.failed }
                stage = "bubble cannot become key"
                guard !bubble.canBecomeKey, !bubble.isKeyWindow else { throw CheckError.failed }
                stage = "bubble preserves key window"
                guard app.keyWindow === keyBeforeReminder else {
                    print("Focus diagnostic: beforeOwn=\(keyBeforeReminder === reopened) afterOwn=\(app.keyWindow === reopened) beforeNil=\(keyBeforeReminder == nil) afterNil=\(app.keyWindow == nil) active=\(app.isActive) foregroundChanged=\(NSWorkspace.shared.frontmostApplication?.processIdentifier != foregroundBefore)")
                    throw CheckError.failed
                }
                stage = "bubble preserves foreground app"
                guard NSWorkspace.shared.frontmostApplication?.processIdentifier == foregroundBefore,
                      app.isActive == activeBefore else { throw CheckError.failed }
                if let bitmap = bubble.contentView?.bitmapImageRepForCachingDisplay(in: bubble.contentView!.bounds) {
                    bubble.contentView!.cacheDisplay(in: bubble.contentView!.bounds, to: bitmap)
                    try bitmap.representation(using: .png, properties: [:])?.write(to: directory.appendingPathComponent("reminder-bubble.png"))
                }
                snooze.performClick(nil)
                try await waitNative { pet.diagnostics["reminder_bubble_visible"] as? Bool == false }
                stage = "snooze persisted"
                let object = try await api("workspace/objects/todo/\(first["id"]!)")
                guard (object["reminders"] as? [[String: Any]])?.first?["status"] as? String == "snoozed" else { throw CheckError.failed }
                stage = "complete bubble"
                let second = try await schedule("原生合成完成提醒")
                try await waitNative { pet.diagnostics["reminder_bubble_visible"] as? Bool == true }
                guard let completeBubble = app.windows.first(where: { $0.title == "拾忆 · 到点提醒" && $0.isVisible }),
                      let complete = button(completeBubble.contentView!, "完成") else { throw CheckError.failed }
                complete.performClick(nil)
                try await waitNative { pet.diagnostics["reminder_bubble_visible"] as? Bool == false }
                stage = "completion persisted"
                let completed = try await api("workspace/objects/todo/\(second["id"]!)")
                guard completed["done"] as? Bool == true else { throw CheckError.failed }
                stage = "shared conversation"
                let third = try await schedule("原生合成共享对话提醒")
                try await waitNative { pet.diagnostics["reminder_bubble_visible"] as? Bool == true }
                guard let openBubble = app.windows.first(where: { $0.title == "拾忆 · 到点提醒" && $0.isVisible }),
                      let open = button(openBubble.contentView!, "打开对话") else { throw CheckError.failed }
                open.performClick(nil)
                try await until(newWeb, "sessionStorage.getItem('workmemory-conversation') === '\(third["conversation_id"]!)'")
                try await until(newWeb, "document.querySelector('.answer-bubble')?.textContent.includes('原生合成共享对话提醒') === true")
                stage = "agent continues while chat is closed"
                _ = try await newWeb.evaluateJavaScript("""
                    (() => { const input = document.querySelector('[aria-label=提问]');
                    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '慢速原生验收');
                    input.dispatchEvent(new Event('input', {bubbles: true})); return true; })()
                    """)
                try await until(newWeb, "!document.querySelector('[aria-label=发送问题]').disabled")
                _ = try await newWeb.evaluateJavaScript("document.querySelector('[aria-label=发送问题]').click(); true")
                try await until(newWeb, "document.querySelector('[aria-label=提问]').value === ''")
                try tapPet(app)
                guard pet.diagnostics["web_retained"] as? Bool == false else { throw CheckError.failed }
                try tapPet(app)
                guard let livePanel = app.windows.first(where: { $0.title == "拾忆 · 和桌宠聊聊" && $0.isVisible }),
                      let liveWeb = livePanel.contentView as? WKWebView else { throw CheckError.failed }
                try await until(liveWeb, "Array.from(document.querySelectorAll('.answer-bubble')).some(el => el.textContent.includes('原生后台回答已完成'))")
                stage = "rapid close during navigation"
                try tapPet(app) // close existing chat
                try tapPet(app) // begin navigation
                try tapPet(app) // close before its document is ready
                guard pet.diagnostics["web_retained"] as? Bool == false else { throw CheckError.failed }
                try tapPet(app)
                guard let finalPanel = app.windows.first(where: { $0.title == "拾忆 · 和桌宠聊聊" && $0.isVisible }),
                      let finalWeb = finalPanel.contentView as? WKWebView else { throw CheckError.failed }
                try await until(finalWeb, "Boolean(document.querySelector('[aria-label=提问]'))")
                stage = "prepare companion draft"
                _ = try await finalWeb.evaluateJavaScript("(() => { const input = document.querySelector('[aria-label=提问]'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '陪伴接话前的草稿'); input.dispatchEvent(new Event('input', {bubbles:true})); })()")
                try await until(finalWeb, "document.querySelector('[aria-label=提问]').value === '陪伴接话前的草稿'")
                try tapPet(app)
                try await Task.sleep(nanoseconds: 500_000_000)
                stage = "proactive bubble without focus"
                _ = try await api("test/proactive", body: [:])
                stage = "wait for proactive bubble"
                let companionForeground = NSWorkspace.shared.frontmostApplication?.processIdentifier
                try await waitNative { pet.diagnostics["companion_bubble_visible"] as? Bool == true }
                stage = "proactive focus assertion"
                guard let companion = app.windows.first(where: { $0.title == "拾忆 · 陪你一会儿" && $0.isVisible }),
                      !companion.canBecomeKey, !companion.isKeyWindow,
                      NSWorkspace.shared.frontmostApplication?.processIdentifier == companionForeground else { throw CheckError.failed }
                stage = "proactive auto dismiss"
                try await waitNative { pet.diagnostics["companion_bubble_visible"] as? Bool == false }
                stage = "proactive click opens shared chat"
                _ = try await api("test/proactive", body: [:])
                try await waitNative { pet.diagnostics["companion_bubble_visible"] as? Bool == true }
                guard let nextBubble = app.windows.first(where: { $0.title == "拾忆 · 陪你一会儿" && $0.isVisible }),
                      let speech = button(nextBubble.contentView!, "本喵正在偷偷给你加油～") else { throw CheckError.failed }
                speech.performClick(nil)
                try await waitNative { pet.diagnostics["chat_open"] as? Bool == true }
                guard let companionChat = app.windows.first(where: { $0.title == "拾忆 · 和桌宠聊聊" && $0.isVisible }),
                      let companionWeb = companionChat.contentView as? WKWebView else { throw CheckError.failed }
                try await until(companionWeb, "Array.from(document.querySelectorAll('.answer-bubble')).some(el => el.textContent.includes('本喵正在偷偷给你加油'))")
                try await until(companionWeb, "document.querySelector('[aria-label=提问]').value === '陪伴接话前的草稿'")
                stage = "reminder preempts proactive"
                try tapPet(app)
                _ = try await api("test/proactive", body: [:])
                try await waitNative { pet.diagnostics["companion_bubble_visible"] as? Bool == true }
                _ = try await schedule("正式提醒优先于陪伴")
                try await waitNative { pet.diagnostics["reminder_bubble_visible"] as? Bool == true }
                guard pet.diagnostics["companion_bubble_visible"] as? Bool == false else { throw CheckError.failed }
                let report: [String: Any] = ["surface": "actual DesktopPet AppKit/WKWebView", "measurements": measurements,
                    "focusBaseline": activeBefore ? "active chat application" : "other foreground application", "nonFocusingReminderBubble": true, "proactiveAutoDismiss": true, "proactiveNonFocusing": true, "proactiveSharedChat": true, "proactiveDraftPreserved": true, "reminderPreemptsProactive": true, "snoozePersisted": true, "completePersisted": true, "reminderSharedConversation": true,
                    "closeDuringLoad": true, "agentContinuesClosed": true, "clickToggle": true, "draftRestored": true, "dragPreservesChat": true, "sharedCustomSkin": true, "firstImportAfterStartup": true, "renderedImportedPNG": true, "sourceSheet": true, "closeReleasedWebView": true, "reopenRestoredConversation": true,
                    "scope": "Isolated PostgreSQL + actual Pi + synthetic model; no desktop capture"]
                try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("report.json"))
                print("Native pet: 3 sizes, chat, sources and reopen passed")
                exit(0)
            } catch {
                // Do not print URL, token, JS text or raw provider output.
                print("Native pet check failed at \(stage): \(type(of: error))")
                exit(1)
            }
        }
        app.run()
    }
    @MainActor static func tapPet(_ app: NSApplication, drag: Bool = false) throws {
        guard let panel = app.windows.first(where: { $0.title == "拾忆桌宠" && $0.isVisible }), let view = panel.contentView else { throw CheckError.failed }
        func event(_ type: NSEvent.EventType, _ x: CGFloat) -> NSEvent {
            NSEvent.mouseEvent(with: type, location: NSPoint(x: x, y: 50), modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                              windowNumber: panel.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
        }
        view.mouseDown(with: event(.leftMouseDown, 50))
        if drag { view.mouseDragged(with: event(.leftMouseDragged, 75)) }
        view.mouseUp(with: event(.leftMouseUp, 50))
    }
    @MainActor static func button(_ view: NSView, _ title: String) -> NSButton? {
        if let found = view as? NSButton, found.title == title { return found }
        for child in view.subviews { if let found = button(child, title) { return found } }
        return nil
    }
    @MainActor static func waitNative(_ predicate: () -> Bool) async throws {
        for _ in 0..<150 {
            if predicate() { return }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw CheckError.failed
    }
    static func api(_ path: String, body: [String: Any]? = nil, method: String = "POST") async throws -> [String: Any] {
        let env = ProcessInfo.processInfo.environment
        var request = URLRequest(url: URL(string: env["WORKMEMORY_ENDPOINT"]! + "/api/" + path)!)
        request.setValue("Bearer " + env["WORKMEMORY_TEST_TOKEN"]!, forHTTPHeaderField: "Authorization")
        if let body {
            request.httpMethod = method
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        if path == "test/proactive" { print("Proactive test trigger HTTP status: \((response as? HTTPURLResponse)?.statusCode ?? 0)") }
        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
              let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw CheckError.failed }
        return value
    }
    static func uploadPNG() async throws -> [String: Any] {
        let env = ProcessInfo.processInfo.environment
        let boundary = UUID().uuidString
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"image\"; filename=\"synthetic.png\"\r\nContent-Type: image/png\r\n\r\n".utf8)
        body.append(try Data(contentsOf: URL(fileURLWithPath: env["WORKMEMORY_TEST_PNG"]!)))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: URL(string: env["WORKMEMORY_ENDPOINT"]! + "/api/pet/skins")!)
        request.httpMethod = "POST"; request.httpBody = body
        request.setValue("Bearer " + env["WORKMEMORY_TEST_TOKEN"]!, forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=" + boundary, forHTTPHeaderField: "Content-Type")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 201,
              let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw CheckError.failed }
        return result
    }
    static func schedule(_ text: String) async throws -> [String: Any] {
        try await api("todos", body: ["request_id": UUID().uuidString, "text": text,
            "due_at": ISO8601DateFormatter().string(from: Date().addingTimeInterval(3)),
            "timezone": "Asia/Shanghai", "repeat": "once", "notify_pet": true])
    }
    enum CheckError: Error { case failed }
    @MainActor static func until(_ web: WKWebView, _ condition: String) async throws {
        for _ in 0..<150 {
            if (try? await web.evaluateJavaScript(condition)) as? Bool == true { return }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw CheckError.failed
    }
}
