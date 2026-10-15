// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "WorkMemory", platforms: [.macOS(.v14)],
    products: [.executable(name: "WorkMemory", targets: ["WorkMemory"])],
    targets: [.target(name: "WorkMemoryCore"), .executableTarget(name: "WorkMemory", dependencies: ["WorkMemoryCore"]),
              .executableTarget(name: "PetCheck", dependencies: ["WorkMemoryCore"], path: "Tests/PetCheck"),
              .executableTarget(name: "OCRCheck", dependencies: ["WorkMemoryCore"], path: "Tests/OCRCheck"),
              .executableTarget(name: "InteractionCheck", dependencies: ["WorkMemoryCore"], path: "Tests/InteractionCheck"),
              .executableTarget(name: "SpoolCheck", dependencies: ["WorkMemoryCore"], path: "Tests/WorkMemoryCoreTests")])
