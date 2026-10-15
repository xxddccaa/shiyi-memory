import Foundation
import WorkMemoryCore

@main struct PetChecks {
    static func main() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let image = folder.appendingPathComponent("cat.png")
        try Data([137, 80, 78, 71]).write(to: image)
        func manifest(_ patch: [String: Any] = [:]) throws {
            var data: [String: Any] = ["version": 1, "name": "测试猫咪", "image": "cat.png", "columns": 2, "rows": 1,
                "animations": ["idle": ["frames": [0, 1], "fps": 12]]]
            for (key, value) in patch { data[key] = value }
            try JSONSerialization.data(withJSONObject: data).write(to: folder.appendingPathComponent("pet.json"))
        }
        try manifest()
        let (skin, url) = try PetSkin.load(folder: folder)
        precondition(skin.name == "测试猫咪" && url == image)
        try manifest(["presentation": "static", "columns": 1, "animations": ["idle": ["frames": [0], "fps": 1]]])
        let staticSkin = try PetSkin.load(folder: folder).0
        precondition(staticSkin.presentation == "static")
        for patch: [String: Any] in [["presentation": "executable"], ["presentation": "static"], ["image": "../cat.png"], ["version": 2], ["rows": 0], ["columns": 1000],
            ["animations": ["idle": ["frames": [2], "fps": 12]]], ["animations": ["idle": ["frames": [0], "fps": 60]]]] {
            try manifest(patch)
            do { _ = try PetSkin.load(folder: folder); fatalError("Invalid skin accepted") } catch {}
        }
        let outside = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        try Data([1]).write(to: outside)
        defer { try? FileManager.default.removeItem(at: outside) }
        try FileManager.default.removeItem(at: image)
        try FileManager.default.createSymbolicLink(at: image, withDestinationURL: outside)
        try manifest()
        do { _ = try PetSkin.load(folder: folder); fatalError("Symlink escaped skin folder") } catch {}
        print("PASS: pet skin schema, bounded animation frames/fps, relative paths and symlink containment")
    }
}
