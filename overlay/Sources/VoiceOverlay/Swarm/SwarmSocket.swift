import Foundation

/// Connects to the Prometheus Swarm hub as a dashboard and keeps trying. The hub's address and the shared token come from
/// `swarm_url.txt` in the data folder (every computer's Prometheus writes it), so there is nothing to type in.
@MainActor
final class SwarmSocket {
    private let model: SwarmModel
    private let urlFile: URL
    private var task: URLSessionWebSocketTask?
    private var retry = 1.0
    private var stopped = false
    /// What the user set on the command line (`--hub http://host:8780/?token=...`), if anything.
    private let override: URL?

    init(model: SwarmModel, urlFile: URL, override: URL? = nil) {
        self.model = model; self.urlFile = urlFile; self.override = override
        model.send = { [weak self] text in self?.task?.send(.string(text)) { _ in } }
    }

    /// The dashboard address as written by Prometheus, with this Mac's own network address turned into "localhost".
    var hubURL: URL? {
        let raw = override ?? (try? String(contentsOf: urlFile, encoding: .utf8)).flatMap { URL(string: $0.trimmingCharacters(in: .whitespacesAndNewlines)) }
        guard let url = raw, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        if let host = parts.host, LocalAddresses.contains(host) { parts.host = "localhost" }
        return parts.url
    }

    func start() { stopped = false; connect() }
    func stop() { stopped = true; task?.cancel(with: .goingAway, reason: nil) }

    private func connect() {
        guard !stopped else { return }
        guard let url = hubURL, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return fail() }
        let token = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "token" })?.value ?? ""
        parts.scheme = (parts.scheme == "https") ? "wss" : "ws"
        parts.path = "/ui"
        parts.queryItems = token.isEmpty ? nil : [URLQueryItem(name: "token", value: token)]
        guard let ws = parts.url else { return fail() }
        let t = URLSession.shared.webSocketTask(with: ws)
        t.maximumMessageSize = 4 * 1024 * 1024
        task = t
        t.resume()
        listen(t)
    }

    private func listen(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.task === t else { return }
                switch result {
                case .success(let msg):
                    if !self.model.connected { self.model.connected = true; self.retry = 1.0 }
                    if case .string(let s) = msg, let data = s.data(using: .utf8), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        self.model.apply(obj)
                    }
                    self.listen(t)
                case .failure:
                    self.fail()
                }
            }
        }
    }

    private func fail() {
        model.connected = false
        task = nil
        guard !stopped else { return }
        let wait = retry
        retry = min(retry * 1.7, 10)
        DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in self?.connect() }
    }
}


/// This Mac's own network addresses, read straight from the system (instant). `Host.current().addresses`, which this replaces, does network
/// lookups and can block the whole app for seconds, so it must never run on the main thread or on every reconnect.
enum LocalAddresses {
    nonisolated(unsafe) private static var cache: (when: Date, list: Set<String>)?

    static func contains(_ host: String) -> Bool {
        if host == "localhost" || host == "127.0.0.1" { return true }
        if let c = cache, Date().timeIntervalSince(c.when) < 30 { return c.list.contains(host) }
        var found = Set<String>()
        var head: UnsafeMutablePointer<ifaddrs>?
        if getifaddrs(&head) == 0 {
            var p = head
            while let a = p {
                let sa = a.pointee.ifa_addr
                if let sa, sa.pointee.sa_family == UInt8(AF_INET) || sa.pointee.sa_family == UInt8(AF_INET6) {
                    var buf = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                    if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &buf, socklen_t(buf.count), nil, 0, NI_NUMERICHOST) == 0 {
                        found.insert(String(cString: buf).components(separatedBy: "%").first ?? "")
                    }
                }
                p = a.pointee.ifa_next
            }
            freeifaddrs(head)
        }
        cache = (Date(), found)
        return found.contains(host)
    }
}
