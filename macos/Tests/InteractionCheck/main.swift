import Foundation
import WorkMemoryCore

func frame(_ time: Double, _ image: String, window: String = "1", bytes: Int = 10, title: String = "合成窗口") -> WindowFrame {
    WindowFrame(png: Data(repeating: 1, count: bytes), fingerprint: image, app: "test", title: title, window: window, time: time)
}
@main struct InteractionChecks {
    static func main() {
        let cache = OCRCache(capacity: 2)
        let staticFrame = frame(0, "exact-pixels")
        precondition(cache.text(for: staticFrame, signature: "model-v1") == nil)
        cache.store("金额 100，尚未完成", for: staticFrame, signature: "model-v1")
        for _ in 0..<120 { precondition(cache.text(for: staticFrame, signature: "model-v1") == "金额 100，尚未完成") }
        precondition(cache.text(for: frame(1, "changed-pixels"), signature: "model-v1") == nil)
        precondition(cache.text(for: frame(1, "exact-pixels", window: "other"), signature: "model-v1") == nil)
        precondition(cache.text(for: frame(1, "exact-pixels", title: "不同标题"), signature: "model-v1") == nil)
        precondition(cache.text(for: staticFrame, signature: "model-v2") == nil)
        cache.removeAll()
        precondition(cache.text(for: staticFrame, signature: "model-v1") == nil)
        print("PASS: exact OCR reuse, critical pixel changes, window/title/model isolation, pause invalidation")
        var schedule = SnapshotSchedule(interval: 5)
        precondition(schedule.take(at: 0, busy: false))
        for quarter in 1..<20 { precondition(!schedule.take(at: Double(quarter) / 4, busy: false)) }
        precondition(schedule.take(at: 5, busy: false))
        precondition(!schedule.take(at: 10, busy: true))
        precondition(!schedule.take(at: 12, busy: false)) // missed tick never catches up
        precondition(schedule.take(at: 15, busy: false))
        precondition(schedule.take(at: 3600, busy: false)) // waking creates just one sample
        precondition(!schedule.take(at: 3600.25, busy: false))
        precondition(schedule.attempts == 5 && schedule.busySkips == 1)
        schedule.updateInterval(10)
        precondition(!schedule.take(at: 3605, busy: false))
        precondition(schedule.take(at: 3610, busy: false))
        precondition(schedule.attempts == 6 && schedule.busySkips == 1)
        precondition(SnapshotSchedule(interval: 1).interval == 5)
        print("PASS: five-second snapshot cadence, busy ticks skipped, no catch-up after sleep")
        let queue = InteractionBuffer()
        queue.receive(frame(0.1, "before"), visual: false)
        queue.trigger("enter", at: 0.2, date: Date())
        queue.receive(frame(0.3, "after"), visual: false)
        queue.finishDue(at: 0.95)
        let group = queue.begin()!
        precondition(group.before!.time < 0.2 && group.after!.time > 0.2)
        queue.finished()
        queue.receive(frame(1.0, "leaving"), visual: false)
        queue.trigger("app_switch", at: 1.1, date: Date())
        queue.receive(frame(1.2, "entering", window: "2"), visual: false)
        queue.finishDue(at: 1.9)
        let switching = queue.begin()!
        precondition(switching.before?.window == "1" && switching.after?.window == "2")
        queue.finished(); queue.clear()
        queue.receive(frame(2, "old"), visual: false)
        queue.trigger("click", at: 3, date: Date())
        queue.receive(frame(3.1, "new"), visual: false)
        queue.finishDue(at: 3.8)
        precondition(queue.begin()!.missingBefore)
        queue.finished(); queue.clear()
        queue.receive(frame(5, "initial"), visual: false)
        queue.trigger("click", at: 5.1, date: Date())
        queue.trigger("enter", at: 5.3, date: Date())
        queue.receive(frame(5.4, "final"), visual: false)
        queue.finishDue(at: 6.1)
        precondition(queue.begin()!.merged == 1)
        queue.finished(); queue.clear()
        precondition(queue.bytes == 0 && queue.pending.isEmpty && queue.active == nil)
        let tabs = InteractionBuffer()
        tabs.receive(frame(10, "tabA", title: "标签A"), visual: false)
        tabs.trigger("window_switch", at: 10.1, date: Date())
        tabs.receive(frame(10.2, "tabB", title: "标签B"), visual: false)
        tabs.finishDue(at: 11)
        let tabGroup = tabs.begin()!
        precondition(tabGroup.after?.title == "标签B")
        tabs.finished()
        tabs.receive(frame(11.1, "loaded", title: "标签B"))
        tabs.finishDue(at: 11.9)
        let supplement = tabs.begin()!
        precondition(supplement.id == tabGroup.id && supplement.supplement)
        let bounded = InteractionBuffer(budget: 35, maxPending: 2)
        for i in 0..<20 {
            let now = Double(i * 3)
            bounded.receive(frame(now, "a\(i)"), visual: false)
            bounded.trigger("app_switch", at: now + 0.1, date: Date())
            bounded.receive(frame(now + 0.2, "b\(i)", window: "\(i+2)"), visual: false)
            bounded.finishDue(at: now + 1)
            precondition(bounded.bytes <= 35 && bounded.pending.count <= 2)
        }
        precondition(bounded.dropped > 0)
        print("PASS: before/after, switching, missing pre-frame, burst merge, pause cleanup, bounded backlog")
    }
}
