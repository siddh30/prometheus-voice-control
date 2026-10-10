// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "VoiceOverlay",
    platforms: [.macOS(.v13)],
    targets: [.executableTarget(name: "VoiceOverlay", path: "Sources/VoiceOverlay")]
)
