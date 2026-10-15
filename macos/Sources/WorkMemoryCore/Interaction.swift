import Foundation

public struct WindowFrame: Sendable {
    public let id: UUID
    public let png: Data
    public let fingerprint: String
    public let app: String
    public let title: String
    public let window: String
    public let time: Double
    public let date: Date
    public init(png: Data, fingerprint: String, app: String, title: String, window: String, time: Double, date: Date = Date()) {
        self.id = UUID(); self.png = png; self.fingerprint = fingerprint
        self.app = app; self.title = title; self.window = window; self.time = time; self.date = date
    }
    public var scope: String { app + ":" + window }
}

public struct InteractionGroup: Sendable {
    public var id = UUID()
    public var trigger: String
    public var eventDate: Date
    public var started: Double
    public var lastEvent: Double
    public var before: WindowFrame?
    public var after: WindowFrame?
    public var merged = 0
    public var supplement = false
    public var missingBefore: Bool { before == nil }
    public var scope: String { (after ?? before)?.scope ?? "" }
}

// Owned by the main actor in the app. A pure state machine for reproducible timing tests.
public final class InteractionBuffer {
    public private(set) var ring: [WindowFrame] = []
    public private(set) var pending: [InteractionGroup] = []
    public private(set) var active: InteractionGroup?
    public private(set) var inFlight: InteractionGroup?
    public private(set) var merged = 0
    public private(set) var dropped = 0
    public private(set) var missingBefore = 0
    private var lastChange = 0.0
    private var lastCompletedFingerprint = ""
    private var supplementFor: (id: UUID, trigger: String, eventDate: Date, scope: String, time: Double)?
    private let merge: Double
    private let maximum: Double
    private let stable: Double
    private let budget: Int
    private let maxPending: Int
    public init(merge: Double = 0.6, maximum: Double = 2, stable: Double = 0.5, budget: Int = 64 * 1024 * 1024, maxPending: Int = 3) {
        self.merge = merge; self.maximum = maximum; self.stable = stable
        self.budget = budget; self.maxPending = maxPending
    }
    public var bytes: Int {
        var images: [UUID: Int] = [:]
        let groups = pending + [active, inFlight].compactMap { $0 }
        for frame in ring + groups.flatMap({ [$0.before, $0.after].compactMap { $0 } }) {
            images[frame.id] = frame.png.count
        }
        return images.values.reduce(0, +)
    }
    public func trigger(_ kind: String, at time: Double, date: Date, window: String? = nil) {
        supplementFor = nil
        if var group = active, kind != "app_switch", kind != "window_switch",
           group.trigger != "app_switch", group.trigger != "window_switch",
           time - group.started < maximum, window == nil || group.scope == window {
            group.lastEvent = time; group.merged += 1; group.after = nil
            active = group; merged += 1
            return
        }
        if active != nil { complete() }
        let before = ring.last { $0.time <= time && time - $0.time <= 0.5 && (window == nil || $0.scope == window) }
        active = InteractionGroup(trigger: kind, eventDate: date, started: time, lastEvent: time, before: before)
        enforceBounds()
    }
    public func receive(_ frame: WindowFrame, visual: Bool = true) {
        let previous = ring.last
        let changed = previous?.fingerprint != frame.fingerprint || previous?.scope != frame.scope
        if changed { lastChange = frame.time }
        if visual && active == nil && changed && frame.fingerprint != lastCompletedFingerprint {
            active = InteractionGroup(trigger: previous == nil ? "resume" : "visual", eventDate: frame.date,
                started: frame.time, lastEvent: frame.time,
                before: previous.flatMap { frame.time - $0.time <= 0.5 && $0.scope == frame.scope ? $0 : nil })
            if let follow = supplementFor, follow.scope == frame.scope, frame.time - follow.time <= 2 {
                active?.id = follow.id; active?.trigger = follow.trigger; active?.eventDate = follow.eventDate
                active?.before = nil; active?.supplement = true; supplementFor = nil
            }
        }
        ring.append(frame)
        if ring.count > 2 { ring.removeFirst(ring.count - 2) }
        if var group = active, frame.time >= group.lastEvent {
            let switches = group.trigger == "app_switch" || group.trigger == "window_switch"
            if group.before == nil || group.before?.scope == frame.scope || switches {
                // A switch needs the entering window; an old-window callback cannot be its after frame.
                if !switches || group.before?.scope != frame.scope || group.before?.title != frame.title { group.after = frame; active = group }
            }
        }
        finishDue(at: frame.time)
        enforceBounds()
    }
    public func finishDue(at time: Double) {
        guard let group = active else { return }
        if time - group.started >= maximum || (group.after != nil && time - group.lastEvent >= merge && time - lastChange >= stable) {
            complete()
        }
    }
    private func complete() {
        guard let group = active else { return }
        active = nil
        guard group.before != nil || group.after != nil else { missingBefore += 1; return }
        if group.missingBefore { missingBefore += 1 }
        lastCompletedFingerprint = group.after?.fingerprint ?? ""
        if !group.supplement {
            supplementFor = (group.id, group.trigger, group.eventDate, group.scope, group.after?.time ?? group.lastEvent)
        }
        // Merge only an adjacent unsent group for the same window. Never replace a switch boundary.
        if let last = pending.last, last.scope == group.scope,
           !["app_switch", "window_switch"].contains(last.trigger),
           !["app_switch", "window_switch"].contains(group.trigger),
           group.started - last.lastEvent < maximum {
            var combined = pending.removeLast()
            combined.after = group.after ?? combined.after
            combined.lastEvent = group.lastEvent
            combined.merged += group.merged + 1
            pending.append(combined); merged += 1
        } else { pending.append(group) }
        enforceBounds()
    }
    private func enforceBounds() {
        while pending.count > maxPending || (bytes > budget && !pending.isEmpty) {
            pending.removeFirst(); dropped += 1
        }
        while bytes > budget && !ring.isEmpty { ring.removeFirst() }
        if bytes > budget && active != nil { active = nil; dropped += 1 }
    }
    public func begin() -> InteractionGroup? {
        guard inFlight == nil, !pending.isEmpty else { return nil }
        inFlight = pending.removeFirst()
        return inFlight
    }
    public func finished() { inFlight = nil }
    public func clear() {
        ring.removeAll(); pending.removeAll(); active = nil; inFlight = nil
        lastCompletedFingerprint = ""; lastChange = 0; supplementFor = nil
    }
}
