import AppKit
import WorkMemoryCore

@main
struct OCRCheck {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { fatalError("Pass the collector config path") }
        let config = try JSONDecoder().decode(CollectorConfig.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let client = try OCRClient(endpoint: config.ocrEndpoint ?? "http://127.0.0.1:8766", token: config.token, timeout: 60)
        let image = NSImage(size: NSSize(width: 1100, height: 260))
        image.lockFocus()
        NSColor.white.setFill()
        NSRect(x: 0, y: 0, width: 1100, height: 260).fill()
        let text = "工作记忆助手 PaddleOCR 2026\n星舟项目上线时间改为周一，等待验收。"
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 36), .foregroundColor: NSColor.black]
        text.draw(in: NSRect(x: 30, y: 35, width: 1040, height: 200), withAttributes: attributes)
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
              let png = bitmap.representation(using: .png, properties: [:]) else { fatalError("Synthetic bitmap failed") }
        let result = try await client.recognize(png: png)
        guard result.engine == "paddleocr", result.text.contains("周一"), result.text.contains("PaddleOCR"), result.text.contains("验收") else {
            fatalError("Synthetic Chinese / English OCR assertions failed")
        }
        print("PASS: Swift memory-only PNG → PaddleOCR → Chinese / English text, \(result.seconds) seconds")
    }
}
