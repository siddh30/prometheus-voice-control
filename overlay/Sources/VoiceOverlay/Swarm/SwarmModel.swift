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

@MainActor
final class SwarmModel: ObservableObject {
    @Published var devices: [SwarmDevice] = []
    @Published var jobs: [SwarmJob] = []
    @Published var connected = false
    @Published var expanded = false
    @Published var typing = false
    var send: ((String) -> Void)?

    var working: Set<String> { Set(jobs.flatMap { $0.subs.filter { $0.state == "running" }.map { $0.node } }) }
    var isWorking: Bool { !working.isEmpty }
    var onlineCount: Int { devices.filter { $0.online }.count }
    var activeJobs: [SwarmJob] { Array(jobs.suffix(3).reversed()) }
    var stateLabel: String {
        if !connected { return "CONNECTING" }
        if isWorking { return "WORKING · \(working.count) DEVICE\(working.count > 1 ? "S" : "")" }
        if devices.isEmpty { return "NO COMPUTERS" }
        return "\(onlineCount) OF \(devices.count) READY"
    }

    // ---- commands ----
    func submit(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        post(["type": "submit", "text": t])
    }
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
            }
        case "job":
            if let j = m["job"] as? [String: Any] {
                let job = Self.job(j)
                if let i = jobs.firstIndex(where: { $0.id == job.id }) { jobs[i] = job } else { jobs.append(job) }
                if job.state == "running" { expanded = true }
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
