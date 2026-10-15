import Foundation
import WorkMemoryCore

@main
struct SpoolChecks {
    static func main() async throws {
        try await testRestartAndAcknowledgement()
        try await testFullQueuePreservesExistingBatch()
        print("PASS: offline restart, acknowledgement, capacity preservation")
    }
    static func testRestartAndAcknowledgement() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = try TextSpool(directory: directory, maxBytes: 10000)
        let event = CaptureEvent(deviceID: "test", app: "editor", title: "合成测试", text: "项目上线改到周一", method: "ax")
        try await first.append(event)
        let restarted = try TextSpool(directory: directory, maxBytes: 10000)
        let next = try await restarted.next()
        precondition(next != nil)
        guard let (file, _) = next else { return }
        do { try await restarted.acknowledge(file: file, ids: []); preconditionFailure("Unacknowledged event was removed") }
        catch SpoolError.invalidAcknowledgement { }
        try await restarted.acknowledge(file: file, ids: [event.id])
        let remaining = try await restarted.files()
        precondition(remaining.isEmpty)
    }
    static func testFullQueuePreservesExistingBatch() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let spool = try TextSpool(directory: directory, maxBytes: 600)
        try await spool.append(CaptureEvent(deviceID: "test", app: "editor", title: "", text: "small", method: "ax"))
        do { try await spool.append(CaptureEvent(deviceID: "test", app: "editor", title: "", text: String(repeating: "大", count: 500), method: "ax")); preconditionFailure("Quota ignored") }
        catch SpoolError.full { }
        let count = try await spool.files().count
        precondition(count == 1)
    }
}
