import Foundation

/// Successful recognition cache only. A hit never acknowledges or skips durable delivery.
/// Owned by the collector's main actor; keys include exact pixel fingerprint and policy context.
public final class OCRCache {
    private struct Key: Hashable {
        let app: String, window: String, title: String, fingerprint: String, signature: String
    }
    private var values: [Key: String] = [:]
    private var order: [Key] = []
    private let capacity: Int
    public private(set) var hits = 0
    public private(set) var misses = 0
    public init(capacity: Int = 8) { self.capacity = max(1, capacity) }
    private func key(_ frame: WindowFrame, _ signature: String) -> Key {
        Key(app: frame.app, window: frame.window, title: frame.title,
            fingerprint: frame.fingerprint, signature: signature)
    }
    public func text(for frame: WindowFrame, signature: String) -> String? {
        let key = key(frame, signature)
        guard let value = values[key] else { misses += 1; return nil }
        hits += 1; order.removeAll { $0 == key }; order.append(key)
        return value
    }
    public func store(_ text: String, for frame: WindowFrame, signature: String) {
        let key = key(frame, signature)
        order.removeAll { $0 == key }; order.append(key); values[key] = text
        while order.count > capacity { values.removeValue(forKey: order.removeFirst()) }
    }
    public func removeAll() { values.removeAll(); order.removeAll() }
}
