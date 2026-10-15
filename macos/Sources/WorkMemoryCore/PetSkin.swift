import Foundation

/// Declarative skins cannot execute code or reference files outside their own folder.
public struct PetSkin: Codable, Sendable {
    public struct Animation: Codable, Sendable {
        public let frames: [Int]
        public let fps: Double
    }
    public let version: Int
    public let name: String
    public let image: String
    public let columns: Int
    public let rows: Int
    public let animations: [String: Animation]
    public let presentation: String?

    public static func load(folder: URL) throws -> (PetSkin, URL) {
        let root = folder.resolvingSymlinksInPath().standardizedFileURL
        let manifest = root.appendingPathComponent("pet.json").resolvingSymlinksInPath()
        guard manifest.deletingLastPathComponent() == root,
              let size = try manifest.resourceValues(forKeys: [.fileSizeKey]).fileSize, size < 65536 else { throw CocoaError(.fileReadCorruptFile) }
        let skin = try JSONDecoder().decode(PetSkin.self, from: Data(contentsOf: manifest))
        guard skin.version == 1, !skin.name.isEmpty, skin.name.count <= 100,
              skin.columns > 0, skin.rows > 0, skin.columns <= 32, skin.rows <= 32,
              !skin.image.contains("/"), skin.image.lowercased().hasSuffix(".png"),
              (skin.presentation == nil || ["static", "atlas"].contains(skin.presentation!)),
              (skin.presentation != "static" || (skin.columns == 1 && skin.rows == 1)),
              !skin.animations.isEmpty, skin.animations["idle"] != nil else { throw CocoaError(.fileReadCorruptFile) }
        let image = root.appendingPathComponent(skin.image).resolvingSymlinksInPath()
        guard image.deletingLastPathComponent() == root,
              let bytes = try image.resourceValues(forKeys: [.fileSizeKey]).fileSize, bytes <= 32 * 1024 * 1024 else { throw CocoaError(.fileReadCorruptFile) }
        for animation in skin.animations.values {
            guard !animation.frames.isEmpty, animation.frames.count <= 128,
                  animation.fps >= 1, animation.fps <= 24,
                  animation.frames.allSatisfy({ $0 >= 0 && $0 < skin.columns * skin.rows }) else { throw CocoaError(.fileReadCorruptFile) }
        }
        return (skin, image)
    }
}
