import Foundation

/// The three apps this switcher chooses between, in the order Ctrl+F12 steps through them. They all share Ctrl+F9, so only one may run at a time.
enum Mode: String, CaseIterable {
    case single, swarm, tv

    var bundleID: String {
        switch self {
        case .single: return "com.siddharth.voicecontrol"
        case .swarm: return "com.siddharth.prometheusswarm"
        case .tv: return "com.siddharth.prometheustv"
        }
    }
    var appName: String {
        switch self {
        case .single: return "Voice Control"
        case .swarm: return "Prometheus Swarm"
        case .tv: return "Prometheus TV"
        }
    }
    var label: String {
        switch self {
        case .single: return "Single"
        case .swarm: return "Swarm"
        case .tv: return "TV"
        }
    }
    /// The next app in the cycle: Single, Swarm, TV, then Single again.
    var next: Mode {
        let all = Mode.allCases
        return all[(all.firstIndex(of: self)! + 1) % all.count]
    }
}

/// What one press of Ctrl+F12 does: quit this one first (if any), then open that one.
struct Plan: Equatable {
    let quit: [Mode]
    let launch: Mode
}

/// Pure decision, no side effects, so it can be tested on its own.
///  * one app running: quit it and open the next one in the cycle (that is the "switch");
///  * nothing running: bring back the one you used last (single mode the first time);
///  * several running (the apps refuse that, but be safe): keep single mode if it is one of them, else the first in the cycle, and quit the rest.
func plan(running: Set<Mode>, last: Mode?) -> Plan {
    let order = Mode.allCases.filter { running.contains($0) }
    if order.count > 1 {
        let keep = running.contains(.single) ? Mode.single : order[0]
        return Plan(quit: order.filter { $0 != keep }, launch: keep)
    }
    if let mode = order.first { return Plan(quit: [mode], launch: mode.next) }
    return Plan(quit: [], launch: last ?? .single)
}
