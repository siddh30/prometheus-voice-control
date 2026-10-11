import Foundation

/// Opening Prometheus Swarm starts its own backend (this computer's swarm node, and the hub if config says so): a small Python process, no
/// microphone and no voice. It is restarted if it dies and stopped when the app quits. Voice Control is not needed on this computer.
final class SwarmBackend {
    private let root: URL
    let dataDir: URL
    private var process: Process?
    private var stopping = false
    private var failures = 0

    init?() {
        let env = ProcessInfo.processInfo.environment["VOICECTL_ROOT"]
        let baked = Bundle.main.resourcePath.flatMap { try? String(contentsOfFile: $0 + "/project_path", encoding: .utf8) }
        guard let path = (env ?? baked)?.trimmingCharacters(in: .whitespacesAndNewlines), !path.isEmpty else { return nil }
        root = URL(fileURLWithPath: path)
        let profile = (try? String(contentsOf: root.appendingPathComponent("profile.txt"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "dev"
        let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/VoiceControl")
        dataDir = profile == "stable" ? support.appendingPathComponent("data") : root.appendingPathComponent("data-\(profile)")
    }

    func start() {
        stopping = false
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        launch()
    }

    func stop() {
        stopping = true
        process?.terminate()
    }

    private func logHandle() -> FileHandle? {
        let url = dataDir.appendingPathComponent("swarm.log")
        let previous = dataDir.appendingPathComponent("swarm.log.prev")
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        return try? FileHandle(forWritingTo: url)
    }

    private func launch() {
        guard !stopping else { return }
        let p = Process()
        p.executableURL = root.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-m", "voicectl.swarm.service"]
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.appendingPathComponent("src").path
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "")         // node, for the browser tools
        p.environment = env
        let log = logHandle()
        p.standardOutput = log
        p.standardError = log
        let started = Date()
        p.terminationHandler = { [weak self] _ in
            guard let self, !self.stopping else { return }
            // A backend that keeps dying right away backs off instead of spinning.
            self.failures = Date().timeIntervalSince(started) < 10 ? min(self.failures + 1, 6) : 0
            DispatchQueue.global().asyncAfter(deadline: .now() + 2 + Double(self.failures) * 5) { self.launch() }
        }
        do { try p.run(); process = p } catch { NSLog("swarm: failed to start the backend: \(error)") }
    }
}
