import AppKit
import ApplicationServices
import ScreenCaptureKit
import WorkMemoryCore

struct CapturedText { let title: String; let text: String; let secure: Bool }
func attribute(_ element: AXUIElement, _ name: CFString) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name, &value) == .success else { return nil }
    return value
}
func captureAX(pid: pid_t) -> CapturedText {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.2)
    if let focused = attribute(app, kAXFocusedUIElementAttribute as CFString), CFGetTypeID(focused) == AXUIElementGetTypeID() {
        let field = unsafeBitCast(focused, to: AXUIElement.self)
        let subrole = attribute(field, kAXSubroleAttribute as CFString) as? String
        if subrole == "AXSecureTextField" { return CapturedText(title: "", text: "", secure: true) }
    }
    guard let value = attribute(app, kAXFocusedWindowAttribute as CFString), CFGetTypeID(value) == AXUIElementGetTypeID() else {
        return CapturedText(title: "", text: "", secure: false)
    }
    let window = unsafeBitCast(value, to: AXUIElement.self)
    let title = attribute(window, kAXTitleAttribute as CFString) as? String ?? ""
    var chunks: [String] = []
    var seen = Set<String>()
    var nodes = 0
    var totalCharacters = 0
    let deadline = Date().addingTimeInterval(3)
    func walk(_ element: AXUIElement, depth: Int) {
        guard depth < 10, nodes < 300, totalCharacters < 16000, Date() < deadline else { return }
        nodes += 1
        let subrole = attribute(element, kAXSubroleAttribute as CFString) as? String
        if subrole == "AXSecureTextField" { return }
        let role = attribute(element, kAXRoleAttribute as CFString) as? String ?? ""
        if ["AXStaticText", "AXTextArea", "AXTextField", "AXHeading", "AXLink"].contains(role) {
            var text = ""
            if let range = attribute(element, kAXVisibleCharacterRangeAttribute as CFString) {
                var visible: CFTypeRef?
                if AXUIElementCopyParameterizedAttributeValue(element, kAXStringForRangeParameterizedAttribute as CFString, range, &visible) == .success {
                    text = visible as? String ?? ""
                }
            }
            if text.isEmpty { text = attribute(element, kAXValueAttribute as CFString) as? String ?? "" }
            if text.isEmpty { text = attribute(element, kAXTitleAttribute as CFString) as? String ?? "" }
            text = String(text.trimmingCharacters(in: .whitespacesAndNewlines).prefix(16000 - totalCharacters))
            if !text.isEmpty && seen.insert(text).inserted { chunks.append(text); totalCharacters += text.count }
        }
        if let children = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] {
            for child in children { walk(child, depth: depth + 1) }
        }
    }
    walk(window, depth: 0)
    return CapturedText(title: title, text: chunks.joined(separator: "\n"), secure: false)
}

struct OCRCapture { let capture: CapturedText; let seconds: Double }

func capturePaddleOCR(pid: pid_t, titleHint: String, client: OCRClient, excludedTitle: (String) -> Bool) async throws -> OCRCapture? {
    guard CGPreflightScreenCaptureAccess() else { return nil }
    let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
    let candidates = content.windows.filter { $0.owningApplication?.processID == pid && $0.windowLayer == 0 && $0.frame.width > 100 && $0.frame.height > 100 }
    // When AX identifies a focused window, do not silently capture another window in the same app.
    let window = titleHint.isEmpty ? candidates.first : candidates.first(where: { $0.title == titleHint })
    guard let window else { return nil }
    let title = window.title ?? ""
    guard !excludedTitle(title), NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { return nil }
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let config = SCStreamConfiguration()
    let scale = min(2, 2400 / window.frame.width, 1800 / window.frame.height)
    config.width = Int(window.frame.width * scale)
    config.height = Int(window.frame.height * scale)
    config.showsCursor = false
    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
    // PNG bytes remain in memory and travel only to the loopback PaddleOCR service.
    guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { throw OCRError.unavailable }
    let result = try await client.recognize(png: png)
    return OCRCapture(capture: CapturedText(title: title, text: String(result.text.prefix(16000)), secure: false), seconds: result.seconds)
}

// Fast metadata query; no recursive text walk on the interaction path.
struct FocusedContext: Equatable, Sendable {
    let pid: pid_t
    let app: String
    let title: String
    let window: UInt32
    let secure: Bool
}
func focusedContext(pid: pid_t, bundle: String) -> FocusedContext? {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.1)
    var secure = false
    if let raw = attribute(app, kAXFocusedUIElementAttribute as CFString), CFGetTypeID(raw) == AXUIElementGetTypeID() {
        let field = unsafeBitCast(raw, to: AXUIElement.self)
        secure = (attribute(field, kAXSubroleAttribute as CFString) as? String) == "AXSecureTextField"
    }
    var title = ""
    if let raw = attribute(app, kAXFocusedWindowAttribute as CFString), CFGetTypeID(raw) == AXUIElementGetTypeID() {
        let window = unsafeBitCast(raw, to: AXUIElement.self)
        title = attribute(window, kAXTitleAttribute as CFString) as? String ?? ""
    }
    // Manual AX mode does not require Screen Recording to resolve window metadata.
    if !CGPreflightScreenCaptureAccess() {
        return FocusedContext(pid: pid, app: bundle, title: title, window: 0, secure: secure)
    }
    let entries = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    let owned = entries.filter {
        ($0[kCGWindowOwnerPID as String] as? Int32) == pid && ($0[kCGWindowLayer as String] as? Int) == 0
    }
    let candidates = title.isEmpty ? Array(owned.prefix(1)) : owned.filter { ($0[kCGWindowName as String] as? String ?? "") == title }
    // CGWindowList is front-to-back: apps without AX titles use their frontmost on-screen window.
    guard candidates.count == 1, let id = candidates[0][kCGWindowNumber as String] as? UInt32 else { return nil }
    if title.isEmpty { title = candidates[0][kCGWindowName as String] as? String ?? "" }
    return FocusedContext(pid: pid, app: bundle, title: title, window: id, secure: secure)
}

import CoreImage
import CryptoKit

/// Timer mode takes one fresh screenshot, without a continuous video stream or frame queue.
@MainActor
func captureWindowFrame(_ context: FocusedContext) async throws -> WindowFrame? {
    guard CGPreflightScreenCaptureAccess(), !context.secure,
          NSWorkspace.shared.frontmostApplication?.processIdentifier == context.pid,
          focusedContext(pid: context.pid, bundle: context.app) == context else { return nil }
    let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
    guard let window = content.windows.first(where: {
        $0.windowID == context.window && $0.owningApplication?.processID == context.pid
    }), window.frame.width > 100, window.frame.height > 100 else { return nil }
    let configuration = SCStreamConfiguration()
    let scale = min(2, 2400 / window.frame.width, 1800 / window.frame.height)
    configuration.width = max(1, Int(window.frame.width * scale))
    configuration.height = max(1, Int(window.frame.height * scale))
    configuration.showsCursor = false; configuration.capturesAudio = false
    let time = ProcessInfo.processInfo.systemUptime
    let date = Date()
    let image = try await SCScreenshotManager.captureImage(
        contentFilter: SCContentFilter(desktopIndependentWindow: window), configuration: configuration)
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == context.pid,
          focusedContext(pid: context.pid, bundle: context.app) == context else { return nil }
    guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]),
          png.count <= 8 * 1024 * 1024 else { throw OCRError.unavailable }
    return WindowFrame(png: png, fingerprint: SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined(),
        app: context.app, title: context.title, window: String(context.window), time: time, date: date)
}

final class FrameStream: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let context: FocusedContext
    private let deliver: @Sendable (WindowFrame) async -> Void
    private var deliveryPending = false
    private let failed: @Sendable () -> Void
    private let queue = DispatchQueue(label: "workmemory.frames", qos: .utility)
    private let renderer = CIContext(options: [.cacheIntermediates: false])
    private var stream: SCStream?
    private var lastPNG: Data?
    private var lastFingerprint = ""
    init(context: FocusedContext, deliver: @escaping @Sendable (WindowFrame) async -> Void, failed: @escaping @Sendable () -> Void) {
        self.context = context; self.deliver = deliver; self.failed = failed
    }
    func start(fps: Int) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
        guard let window = content.windows.first(where: { $0.windowID == context.window && $0.owningApplication?.processID == context.pid }) else { throw OCRError.unavailable }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        let scale = min(2, 2400 / window.frame.width, 1800 / window.frame.height)
        config.width = max(1, Int(window.frame.width * scale)); config.height = max(1, Int(window.frame.height * scale))
        config.minimumFrameInterval = CMTime(value: 1, timescale: Int32(max(1, min(4, fps))))
        config.queueDepth = 3; config.showsCursor = false; config.capturesAudio = false
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        self.stream = stream
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try await stream.startCapture()
    }
    func stop() async {
        try? await stream?.stopCapture()
        stream = nil
        await withCheckedContinuation { continuation in
            queue.async { self.lastPNG = nil; self.renderer.clearCaches(); continuation.resume() }
        }
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) { failed() }
    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sample.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let rawStatus = attachments.first?[.status] as? Int,
              let status = SCFrameStatus(rawValue: rawStatus), [.complete, .idle].contains(status) else { return }
        guard !deliveryPending else { return }
        // Check secure focus before handling each frame, including cached idle frames.
        let element = AXUIElementCreateApplication(context.pid)
        AXUIElementSetMessagingTimeout(element, 0.1)
        if let raw = attribute(element, kAXFocusedUIElementAttribute as CFString), CFGetTypeID(raw) == AXUIElementGetTypeID() {
            let field = unsafeBitCast(raw, to: AXUIElement.self)
            if (attribute(field, kAXSubroleAttribute as CFString) as? String) == "AXSecureTextField" {
                lastPNG = nil; failed(); return
            }
        }
        autoreleasepool {
            if status == .complete {
                guard let pixel = sample.imageBuffer,
                      let image = renderer.createCGImage(CIImage(cvPixelBuffer: pixel), from: CGRect(x: 0, y: 0, width: CVPixelBufferGetWidth(pixel), height: CVPixelBufferGetHeight(pixel))),
                      let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]), png.count <= 8 * 1024 * 1024 else { return }
                lastPNG = png
                lastFingerprint = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined()
            }
            guard let png = lastPNG else { return }
            let time = CMSampleBufferGetPresentationTimeStamp(sample).seconds
            guard time.isFinite else { return }
            let frame = WindowFrame(png: png, fingerprint: lastFingerprint, app: context.app, title: context.title,
                window: String(context.window), time: time, date: Date(timeIntervalSinceNow: time - ProcessInfo.processInfo.systemUptime))
            deliveryPending = true
            Task {
                await deliver(frame)
                queue.async { self.deliveryPending = false }
            }
        }
    }
}

/// Optional URL metadata, limited to the focused Safari/Chrome window. No scripts or extension.
func browserResource(pid: pid_t, bundle: String) -> String? {
    guard ["com.apple.Safari", "com.google.Chrome"].contains(bundle) else { return nil }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.05)
    guard let raw = attribute(app, kAXFocusedWindowAttribute as CFString), CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
    let window = unsafeBitCast(raw, to: AXUIElement.self)
    var queue: [(AXUIElement, Int)] = [(window, 0)]
    var count = 0
    while !queue.isEmpty && count < 80 {
        let (element, depth) = queue.removeFirst(); count += 1
        AXUIElementSetMessagingTimeout(element, 0.02)
        for key in ["AXURL", "AXDocument"] {
            let raw = attribute(element, key as CFString)
            let text = (raw as? URL)?.absoluteString ?? (raw as? String)
            if let text, var url = URLComponents(string: text), ["https", "http"].contains(url.scheme ?? ""), url.host != nil {
                url.query = nil; url.fragment = nil; url.user = nil; url.password = nil
                return url.string.map { String($0.prefix(2000)) }
            }
        }
        if depth < 5, let children = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] {
            queue.append(contentsOf: children.prefix(20).map { ($0, depth + 1) })
        }
    }
    return nil
}
