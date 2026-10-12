import Foundation

/// When running inside Voice Control.app, the app owns the Python daemon (and Phoenix) so a single
/// Dock icon starts and stops everything. The project root is baked into the bundle at build time.
final class DaemonSupervisor {
    private let root: URL
    let profile: String                        // stable (Voice Control) | swarm (Prometheus Swarm) | dev
    /// Where this app's daemon listens (the ports are in config.yaml / config.swarm.yaml; a test keeps them equal).
    var wsURL: URL { URL(string: profile == "swarm" ? "ws://127.0.0.1:8767" : "ws://127.0.0.1:8765")! }
    private let dataDir: URL                   // where logs and memory live: per profile, never inside a release folder
    private let phoenixDir: URL
    private var daemon: Process?
    private var phoenix: Process?
    private var stopping = false

    init?() {
        let env = ProcessInfo.processInfo.environment["VOICECTL_ROOT"]
        let baked = Bundle.main.resourcePath.flatMap { try? String(contentsOfFile: $0 + "/project_path", encoding: .utf8) }
        guard let path = (env ?? baked)?.trimmingCharacters(in: .whitespacesAndNewlines), !path.isEmpty else { return nil }
        root = URL(fileURLWithPath: path)
        profile = (try? String(contentsOf: root.appendingPathComponent("profile.txt"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "dev"
        let base = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        let support = base.appendingPathComponent(profile == "swarm" ? "PrometheusSwarm" : "VoiceControl")
        let installed = profile == "stable" || profile == "swarm"
        dataDir = installed ? support.appendingPathComponent("data") : root.appendingPathComponent("data-\(profile)")
        phoenixDir = installed ? support.appendingPathComponent("phoenix_data") : root.appendingPathComponent("phoenix_data")
    }

    func start() {
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        startPhoenixIfNeeded()
        launchDaemon()
    }

    func stop() {
        stopping = true
        daemon?.terminate()
        phoenix?.terminate()
    }

    func restartDaemon() {
        daemon?.terminate()   // terminationHandler relaunches it
    }

    private func logHandle(_ name: String) -> FileHandle? {
        let url = dataDir.appendingPathComponent(name)
        let previous = dataDir.appendingPathComponent(name + ".prev")          // keep the last run's log: that is where a "stuck" shows
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        return try? FileHandle(forWritingTo: url)
    }

    private func launchDaemon() {
        let p = Process()
        p.executableURL = root.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-m", "voicectl.main"]
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = root.appendingPathComponent("src").path
        env["PYTHONUNBUFFERED"] = "1"
        env["VOICECTL_HOTKEYS"] = "app"      // the app captures keys and pastes; the daemon just reacts
        p.environment = env
        let log = logHandle("daemon.log")
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] _ in
            guard let self, !self.stopping else { return }
            DispatchQueue.global().asyncAfter(deadline: .now() + 2) { self.launchDaemon() }
        }
        do { try p.run(); daemon = p } catch { NSLog("voicectl: failed to start daemon: \(error)") }
    }

    private func startPhoenixIfNeeded() {
        var req = URLRequest(url: URL(string: "http://localhost:6006/healthz")!)
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            guard let self, (resp as? HTTPURLResponse)?.statusCode != 200 else { return }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/sh")
            p.arguments = [self.root.appendingPathComponent("scripts/phoenix.sh").path]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "")
            env["VOICECTL_PHOENIX_DIR"] = self.phoenixDir.path
            p.environment = env
            let log = self.logHandle("phoenix.log")
            p.standardOutput = log
            p.standardError = log
            try? p.run()
            self.phoenix = p
        }.resume()
    }
}
