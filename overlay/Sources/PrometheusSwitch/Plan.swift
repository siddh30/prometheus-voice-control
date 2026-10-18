import Foundation

/// The two apps this switcher chooses between. They share Ctrl+F9 and Ctrl+F10, so only one may run at a time.
enum Mode: String, CaseIterable {
    case single, swarm

    var bundleID: String { self == .single ? "com.siddharth.voicecontrol" : "com.siddharth.prometheusswarm" }
    var appName: String { self == .single ? "Voice Control" : "Prometheus Swarm" }
    var label: String { self == .single ? "Single" : "Swarm" }
    var other: Mode { self == .single ? .swarm : .single }
}

/// What one press of Ctrl+F12 does: quit this one first (if any), then open that one.
struct Plan: Equatable {
    let quit: [Mode]
    let launch: Mode
}

/// Pure decision, no side effects, so it can be tested on its own.
///  * one app running: quit it and open the other (that is the "switch");
///  * nothing running: bring back the one you used last (single mode the first time);
///  * both running (the apps refuse that, but be safe): keep single mode, quit the swarm.
func plan(running: Set<Mode>, last: Mode?) -> Plan {
    if running.contains(.single) && running.contains(.swarm) { return Plan(quit: [.swarm], launch: .single) }
    if let mode = running.first { return Plan(quit: [mode], launch: mode.other) }
    return Plan(quit: [], launch: last ?? .single)
}
