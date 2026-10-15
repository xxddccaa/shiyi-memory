import Foundation

/// A due tick is consumed even while OCR is busy: never replay missed screenshots.
public struct SnapshotSchedule {
    public private(set) var interval: Double
    private var lastTick: Double?
    public private(set) var attempts = 0
    public private(set) var busySkips = 0

    public init(interval: Double = 5) { self.interval = max(5, interval) }

    public mutating func updateInterval(_ value: Double) { interval = max(5, value) }

    public mutating func take(at time: Double, busy: Bool) -> Bool {
        if let lastTick, time - lastTick < interval { return false }
        lastTick = time
        attempts += 1
        if busy { busySkips += 1 }
        return !busy
    }
}
