// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "VoiceOverlay",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "VoiceOverlay", path: "Sources/VoiceOverlay"),
        // the tiny F12 app that switches between Voice Control and Prometheus Swarm (no daemon, no UI beyond a menu-bar item)
        .executableTarget(name: "PrometheusSwitch", path: "Sources/PrometheusSwitch"),
    ]
)
