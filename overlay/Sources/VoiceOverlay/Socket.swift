import Foundation

/// Everything the daemon says lands here first, off the main thread. The widget's animations never wait on it:
/// a 30 Hz tick on the main thread takes whatever has arrived (the mic level collapsed to its latest value) and
/// applies it in one go. A flood of messages becomes at most 30 small updates a second; silence changes nothing.
final class Ingest {
    private let lock = NSLock()
    private var queue: [(String, [String: Any])] = []
    private var level: Double?
    private var heard = Date()
    private var link = false

    func push(type: String, data: [String: Any]) {
        lock.lock(); defer { lock.unlock() }
        heard = Date(); link = true
        if type == "level" { level = (data["value"] as? Double) ?? 0 }
        else if type != "heartbeat" { queue.append((type, data)) }
    }
    func setLink(_ up: Bool) { lock.lock(); link = up; if up { heard = Date() }; lock.unlock() }

    func take() -> (events: [(String, [String: Any])], level: Double?, silent: TimeInterval, link: Bool) {
        lock.lock(); defer { lock.unlock() }
        let out = (queue, level, Date().timeIntervalSince(heard), link)
        queue = []; level = nil
        return out
    }
}

/// Reconnecting WebSocket client for the voicectl daemon.
final class DaemonSocket {
    private let ingest = Ingest()
    private let screenQueue = DispatchQueue(label: "voicectl.screen")     // screenshots, mouse and keys never run on the UI thread
    private var tick: DispatchSourceTimer?
    private let url: URL
    private var task: URLSessionWebSocketTask?
    private let model: OverlayModel
    var onConfig: (([String], String) -> Void)?

    init(url: URL, model: OverlayModel) {
        self.url = url
        self.model = model
        Task { @MainActor in model.send = { [weak self] text in self?.send(text) } }
        connect()
        startTick()
    }

    private func startTick() {
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now(), repeating: .milliseconds(33), leeway: .milliseconds(5))
        t.setEventHandler { [weak self] in
            guard let self else { return }
            let batch = self.ingest.take()
            MainActor.assumeIsolated {
                if self.model.connected != batch.link { self.model.connected = batch.link }
                let stalled = batch.link && batch.silent > 8          // the daemon sends a heartbeat every 3 s
                if self.model.stalled != stalled { self.model.stalled = stalled }
                if let v = batch.level { self.model.handle(type: "level", data: ["value": v]) }
                for (type, data) in batch.events { self.model.handle(type: type, data: data) }
            }
        }
        t.resume()
        tick = t
    }

    func send(_ text: String) {
        task?.send(.string(text)) { _ in }
    }

    private func connect() {
        let t = URLSession.shared.webSocketTask(with: url)
        task = t
        t.resume()
        receive(t)
    }

    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let msg):
                if case .string(let s) = msg,
                   let d = s.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                   let type = obj["type"] as? String {
                    let data = (obj["data"] as? [String: Any]) ?? [:]
                    switch type {
                    case "config":
                        let hk = (data["hotkeys"] as? [String: String]).map { Array($0.values) } ?? []
                        let cancel = (data["cancel"] as? String) ?? "esc"
                        DispatchQueue.main.async { self.onConfig?(hk, cancel) }
                    case "input_request":      // the agent wants to see the screen / move the mouse / press keys
                        let id = data["id"] as? String ?? ""
                        screenQueue.async {
                            var res = ScreenControl.handle(data)
                            res["type"] = "input_result"
                            res["id"] = id
                            if let d = try? JSONSerialization.data(withJSONObject: res), let text = String(data: d, encoding: .utf8) {
                                self.send(text)
                            }
                        }
                    case "paste":
                        if let text = data["text"] as? String { DispatchQueue.main.async { Paster.paste(text) } }
                    default:
                        self.ingest.push(type: type, data: data)
                    }
                    if type == "config" || type == "input_request" || type == "paste" { self.ingest.push(type: "heartbeat", data: [:]) }
                }
                self.receive(t)
            case .failure:
                self.ingest.setLink(false)
                DispatchQueue.global().asyncAfter(deadline: .now() + 1.5) { self.connect() }
            }
        }
    }
}
