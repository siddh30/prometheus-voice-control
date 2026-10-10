import Foundation

/// Reconnecting WebSocket client for the voicectl daemon.
final class DaemonSocket {
    private let url: URL
    private var task: URLSessionWebSocketTask?
    private let model: OverlayModel
    var onConfig: (([String], String) -> Void)?

    init(url: URL, model: OverlayModel) {
        self.url = url
        self.model = model
        Task { @MainActor in model.send = { [weak self] text in self?.send(text) } }
        connect()
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
                Task { @MainActor in self.model.connected = true }   // only once a message really arrived
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
                        DispatchQueue.main.async {
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
                        Task { @MainActor in self.model.handle(type: type, data: data) }
                    }
                }
                self.receive(t)
            case .failure:
                Task { @MainActor in self.model.connected = false }
                DispatchQueue.global().asyncAfter(deadline: .now() + 1.5) { self.connect() }
            }
        }
    }
}
