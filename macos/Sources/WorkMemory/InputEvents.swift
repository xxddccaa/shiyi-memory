import AppKit
import ApplicationServices

final class InputEvents {
    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private let deliver: (String, Double) -> Void
    init(deliver: @escaping (String, Double) -> Void) { self.deliver = deliver }
    func start() -> Bool {
        if tap != nil { return true }
        let types: [CGEventType] = [.keyDown, .leftMouseDown, .rightMouseDown]
        let mask = types.reduce(CGEventMask(0)) { $0 | (1 << $1.rawValue) }
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
            eventsOfInterest: mask, callback: { _, type, event, pointer in
                guard let pointer else { return Unmanaged.passUnretained(event) }
                let owner = Unmanaged<InputEvents>.fromOpaque(pointer).takeUnretainedValue()
                if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                    if let tap = owner.tap { CGEvent.tapEnable(tap: tap, enable: true) }
                } else if type == .keyDown {
                    // Only the Enter key code is inspected. No typed characters or other keys are retained.
                    let key = event.getIntegerValueField(.keyboardEventKeycode)
                    if key == 36 || key == 76 { owner.deliver("enter", Double(event.timestamp) / 1_000_000_000) }
                } else if type == .leftMouseDown || type == .rightMouseDown {
                    owner.deliver("click", Double(event.timestamp) / 1_000_000_000)
                }
                return Unmanaged.passUnretained(event)
            }, userInfo: Unmanaged.passUnretained(self).toOpaque()) else { return false }
        self.tap = tap
        source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        return true
    }
    func stop() {
        if let tap { CGEvent.tapEnable(tap: tap, enable: false); CFMachPortInvalidate(tap) }
        if let source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        tap = nil; source = nil
    }
    deinit { stop() }
}

final class FocusNotifications {
    private var observer: AXObserver?
    private let changed: () -> Void
    init(changed: @escaping () -> Void) { self.changed = changed }
    func watch(pid: pid_t) {
        stop()
        var value: AXObserver?
        guard AXObserverCreate(pid, { _, _, _, pointer in
            guard let pointer else { return }
            Unmanaged<FocusNotifications>.fromOpaque(pointer).takeUnretainedValue().changed()
        }, &value) == .success, let value else { return }
        observer = value
        let element = AXUIElementCreateApplication(pid)
        for name in [kAXFocusedWindowChangedNotification, kAXFocusedUIElementChangedNotification, kAXTitleChangedNotification] {
            AXObserverAddNotification(value, element, name as CFString, Unmanaged.passUnretained(self).toOpaque())
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(value), .commonModes)
    }
    func stop() {
        if let observer { CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes) }
        observer = nil
    }
    deinit { stop() }
}
