import Foundation

public struct CollectorConfig: Decodable {
    public let endpoint: String
    public let token: String
    public let spoolPath: String
    public let intervalSeconds: Double
    public let maxSpoolBytes: Int
    public let statusPath: String?
    public let ocrEndpoint: String?
    public let ocrSignature: String?
    public let ocrMode: String?
    public let ocrTimeoutSeconds: Double?
    public let captureMode: String?
    public let frameRate: Int?
    public let mergeSeconds: Double?
    public let maxGroupSeconds: Double?
    public let stableSeconds: Double?
    public let ocrMinInterval: Double?
    public let imageBudgetBytes: Int?
    public let maxPendingGroups: Int?
    public let captureStatePath: String?
    public let excludedApps: [String]
    public let excludedTitlePatterns: [String]
}

public struct CaptureEvent: Codable, Sendable {
    public let id: String
    public let device_id: String
    public let captured_at: String
    public let app: String
    public let title: String
    public let text: String
    public let method: String
    public let interaction_id: String?
    public let trigger: String?
    public let phase: String?
    public let window_id: String?
    public let event_at: String?
    public let missing_before: Bool?
    public let missing_after: Bool?
    public let merged_count: Int?
    public let resource_uri: String?
    public let dropped_groups: Int?
    public init(id: String = UUID().uuidString.lowercased(), deviceID: String, app: String, title: String, text: String, method: String, capturedAt: Date = Date(), interactionID: String? = nil,
                trigger: String? = nil, phase: String? = nil, windowID: String? = nil, eventAt: Date? = nil,
                missingBefore: Bool? = nil, missingAfter: Bool? = nil, mergedCount: Int? = nil, droppedGroups: Int? = nil, resourceURI: String? = nil) {
        self.id = id
        self.resource_uri = resourceURI
        self.device_id = deviceID
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        self.captured_at = formatter.string(from: capturedAt)
        self.interaction_id = interactionID; self.trigger = trigger; self.phase = phase
        self.window_id = windowID; self.event_at = eventAt.map { formatter.string(from: $0) }
        self.missing_before = missingBefore; self.missing_after = missingAfter
        self.merged_count = mergedCount; self.dropped_groups = droppedGroups
        self.app = app
        self.title = String(title.prefix(1000))
        self.text = String(text.prefix(30000))
        self.method = method
    }
}
public struct EventBatch: Codable, Sendable { public let events: [CaptureEvent] }
public enum SpoolError: Error { case full, invalidAcknowledgement }

public actor TextSpool {
    public let directory: URL
    private let maxBytes: Int
    public init(directory: URL, maxBytes: Int) throws {
        self.directory = directory
        self.maxBytes = maxBytes
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    public func files() throws -> [URL] {
        try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.fileSizeKey])
            .filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
    public func bytes() throws -> Int {
        try files().reduce(0) { $0 + ((try $1.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 0) }
    }
    public func append(_ event: CaptureEvent) throws {
        let data = try JSONEncoder().encode(EventBatch(events: [event]))
        guard try bytes() + data.count <= maxBytes else { throw SpoolError.full }
        let filename = String(format: "%016.0f", Date().timeIntervalSince1970 * 1000) + "-" + event.id + ".json"
        let target = directory.appendingPathComponent(filename)
        try data.write(to: target, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
    }
    public func next() throws -> (URL, Data)? {
        guard let file = try files().first else { return nil }
        return (file, try Data(contentsOf: file))
    }
    public func acknowledge(file: URL, ids: Set<String>) throws {
        guard file.deletingLastPathComponent().standardizedFileURL == directory.standardizedFileURL else {
            throw SpoolError.invalidAcknowledgement
        }
        let batch = try JSONDecoder().decode(EventBatch.self, from: Data(contentsOf: file))
        guard batch.events.allSatisfy({ ids.contains($0.id) }) else { throw SpoolError.invalidAcknowledgement }
        try FileManager.default.removeItem(at: file)
    }
}
