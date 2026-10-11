import Foundation
import SwiftUI

// Prometheus Swarm: what the hub tells us (computers, requests, who is doing what), ready for the widget to draw.

struct SwarmDevice: Identifiable, Equatable {
    var name: String
    var status: String          // idle | busy | offline
    var online: Bool
    var description: String = ""
    var id: String { name }
}

struct SwarmStep: Equatable { var text: String; var done: Bool; var failed: Bool }
struct SwarmCheck: Equatable { var text: String; var status: String }

struct SwarmSub: Identifiable, Equatable {
    var id: String
    var job: String
    var node: String
    var why: String
    var state: String           // queued | running | done | failed | cancelled
    var steps: [SwarmStep]
    var checklist: [SwarmCheck]
    var result: String
    var approval: String
}

struct SwarmJob: Identifiable, Equatable {
    var id: String
    var request: String
    var state: String           // running | done | failed | cancelled
    var result: String
    var subs: [SwarmSub]
}

/// Talks to the hub as a dashboard and keeps what it says: which computers are online, and the requests with each computer's progress.
/// The Prometheus widget shows it (the devices map and the latest request) while Swarm mode is on.
@MainActor
final class SwarmModel: ObservableObject {
    @Published var devices: [SwarmDevice] = []
    @Published var jobs: [SwarmJob] = []
    @Published var connected = false
    @Published var targets: Set<String> = []           // the computers you picked by hand; none = let the swarm decide
    var send: ((String) -> Void)?
    var onTargets: (([String]) -> Void)?                // tells Prometheus (the daemon) where to send the next request
    private var hidden: Set<String> = []

    var working: Set<String> { Set(jobs.flatMap { $0.subs.filter { $0.state == "running" }.map { $0.node } }) }
    var isWorking: Bool { !working.isEmpty }
    var onlineCount: Int { devices.filter { $0.online }.count }

    /// Only the latest request is shown, so the widget never keeps growing.
    var latestJob: SwarmJob? { jobs.last { !hidden.contains($0.id) } }
    var moreRunning: Int { jobs.filter { $0.state == "running" && $0.id != latestJob?.id }.count }
    /// Called when a new session starts: what came before is tucked away.
    func newSession() { hidden.formUnion(jobs.map { $0.id }) }

    /// Tap a computer to send your requests there (tap again to un-pick); only computers that are online can be picked.
    func toggleTarget(_ name: String) {
        guard let d = devices.first(where: { $0.name == name }), d.online else { return }
        if targets.contains(name) { targets.remove(name) } else { targets.insert(name) }
        onTargets?(targets.sorted())
    }
    func clearTargets() { targets = []; onTargets?([]) }

    // ---- commands ----
    func cancel(_ job: String) { post(["type": "cancel", "job": job]) }
    func approve(_ sub: String, _ ok: Bool) { post(["type": "approve", "sub": sub, "ok": ok]) }
    private func post(_ obj: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: data, encoding: .utf8) else { return }
        send?(s)
    }

    // ---- what the hub says ----
    func apply(_ m: [String: Any]) {
        switch m["type"] as? String {
        case "snapshot":
            devices = (m["nodes"] as? [[String: Any]] ?? []).map(Self.device)
            jobs = (m["jobs"] as? [[String: Any]] ?? []).map(Self.job)
        case "node":
            if let n = m["node"] as? [String: Any] {
                let d = Self.device(n)
                if let i = devices.firstIndex(where: { $0.name == d.name }) { devices[i] = d } else { devices.append(d); devices.sort { $0.name < $1.name } }
                if !d.online, targets.remove(d.name) != nil { onTargets?(targets.sorted()) }       // a picked computer that went offline is un-picked
            }
        case "job":
            if let j = m["job"] as? [String: Any] {
                let job = Self.job(j)
                if let i = jobs.firstIndex(where: { $0.id == job.id }) { jobs[i] = job } else { jobs.append(job) }
            }
        case "sub":
            if let s = m["sub"] as? [String: Any] {
                let sub = Self.sub(s)
                if let j = jobs.firstIndex(where: { $0.id == sub.job }) {
                    if let i = jobs[j].subs.firstIndex(where: { $0.id == sub.id }) { jobs[j].subs[i] = sub } else { jobs[j].subs.append(sub) }
                }
            }
        default: break
        }
    }

    static func device(_ n: [String: Any]) -> SwarmDevice {
        let online = (n["online"] as? Bool) ?? true
        return SwarmDevice(name: n["name"] as? String ?? "?", status: online ? (n["status"] as? String ?? "idle") : "offline",
                           online: online, description: n["description"] as? String ?? "")
    }
    static func sub(_ s: [String: Any]) -> SwarmSub {
        SwarmSub(id: s["id"] as? String ?? "", job: s["job"] as? String ?? "", node: s["node"] as? String ?? "", why: s["why"] as? String ?? "",
                 state: s["state"] as? String ?? "queued",
                 steps: (s["steps"] as? [[String: Any]] ?? []).map { SwarmStep(text: $0["text"] as? String ?? "", done: $0["done"] as? Bool ?? false, failed: $0["failed"] as? Bool ?? false) },
                 checklist: (s["checklist"] as? [[String: Any]] ?? []).map { SwarmCheck(text: $0["text"] as? String ?? "", status: $0["status"] as? String ?? "pending") },
                 result: s["result"] as? String ?? "", approval: s["approval"] as? String ?? "")
    }
    static func job(_ j: [String: Any]) -> SwarmJob {
        SwarmJob(id: j["id"] as? String ?? "", request: j["request"] as? String ?? "", state: j["state"] as? String ?? "running",
                 result: j["result"] as? String ?? "", subs: (j["subs"] as? [[String: Any]] ?? []).map(sub))
    }
}
