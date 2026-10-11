import AppKit
import Combine
import SwiftUI

/// The Prometheus Swarm app: its own Dock icon and floating widget, no voice and no agent of its own. It only shows the swarm and
/// lets you send it work; the hub (on one of your computers) does the assigning.
@MainActor
final class SwarmAppDelegate: NSObject, NSApplicationDelegate {
    let model = SwarmModel()
    var panel: OverlayPanel!
    var socket: SwarmSocket!
    var backend: SwarmBackend?
    var typingWatch: AnyCancellable?
    var anchorX: CGFloat?, anchorY: CGFloat?
    var placing = false
    static let kX = "swarm.anchorX", kY = "swarm.anchorY"

    static func argument(_ name: String) -> String? {
        guard let i = CommandLine.arguments.firstIndex(of: name), i + 1 < CommandLine.arguments.count else { return nil }
        return CommandLine.arguments[i + 1]
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        // Inside the .app this starts the swarm backend itself (no Voice Control needed); from the command line it only shows the widget.
        if !CommandLine.arguments.contains("--no-backend"), Bundle.main.bundlePath.hasSuffix(".app") || CommandLine.arguments.contains("--backend") {
            backend = SwarmBackend()
            backend?.start()
        }
        let data = Self.argument("--data").map { URL(fileURLWithPath: $0) }
            ?? backend?.dataDir
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/VoiceControl/data")
        socket = SwarmSocket(model: model, urlFile: data.appendingPathComponent("swarm_url.txt"), override: Self.argument("--hub").flatMap(URL.init(string:)))

        let hosting = NSHostingView(rootView: SwarmWidget(model: model, onSize: { [weak self] size in
            DispatchQueue.main.async { self?.place(size: CGSize(width: size.width + 32, height: size.height + 32)) }
        }))
        panel = OverlayPanel(contentRect: NSRect(x: 0, y: 0, width: 300, height: 60), styleMask: [.borderless, .nonactivatingPanel],
                             backing: .buffered, defer: false)
        panel.allowKey = true                              // you type into its box: it may become the key window (without activating the app)
        panel.contentView = hosting
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isMovableByWindowBackground = true
        let d = UserDefaults.standard
        if d.object(forKey: Self.kX) != nil { anchorX = CGFloat(d.double(forKey: Self.kX)); anchorY = CGFloat(d.double(forKey: Self.kY)) }
        place(size: CGSize(width: 320, height: 64))
        panel.orderFrontRegardless()
        typingWatch = model.$typing.sink { [weak self] on in if on { self?.panel.makeKey() } }
        NotificationCenter.default.addObserver(self, selector: #selector(userMoved), name: NSWindow.didMoveNotification, object: panel)
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        socket.start()
    }

    /// Stays put where you dragged it; the window hugs the widget and grows upward from its bottom edge.
    func place(size: CGSize) {
        guard let screen = panel.screen ?? NSScreen.main else { return }
        let vf = screen.visibleFrame
        let ax = anchorX ?? (vf.maxX - size.width / 2 - 12), ay = anchorY ?? (vf.minY + 12)
        var x = ax - size.width / 2, y = ay
        x = min(max(x, vf.minX), vf.maxX - size.width)
        y = max(min(y, vf.maxY - size.height), vf.minY)
        placing = true
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
        placing = false
    }

    @objc func userMoved() {
        guard !placing else { return }
        anchorX = panel.frame.midX; anchorY = panel.frame.minY
        UserDefaults.standard.set(Double(anchorX!), forKey: Self.kX); UserDefaults.standard.set(Double(anchorY!), forKey: Self.kY)
    }
    @objc func screensChanged() { place(size: panel.frame.size) }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let m = NSMenu()
        m.addItem(withTitle: "Open the dashboard in a browser", action: #selector(openBrowser), keyEquivalent: "").target = self
        m.addItem(withTitle: "Reset widget position", action: #selector(resetPosition), keyEquivalent: "").target = self
        return m
    }
    @objc func openBrowser() { if let u = socket.hubURL { NSWorkspace.shared.open(u) } }
    @objc func resetPosition() {
        anchorX = nil; anchorY = nil
        UserDefaults.standard.removeObject(forKey: Self.kX); UserDefaults.standard.removeObject(forKey: Self.kY)
        place(size: panel.frame.size)
    }
    func applicationWillTerminate(_ notification: Notification) { backend?.stop() }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { panel.orderFrontRegardless(); model.expanded = true; return true }
}

// ---- icon ----
struct SwarmIconView: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.10, green: 0.11, blue: 0.20), Color(red: 0.03, green: 0.03, blue: 0.07)], startPoint: .top, endPoint: .bottom))
            RoundedRectangle(cornerRadius: 185, style: .continuous).stroke(.white.opacity(0.12), lineWidth: 6)
            ZStack {
                ForEach(0..<3) { i in
                    let a = -Double.pi / 2 + 2 * Double.pi * Double(i) / 3
                    let p = CGPoint(x: 400 + 215 * cos(a), y: 400 + 215 * sin(a))
                    Path { path in path.move(to: CGPoint(x: 400, y: 400)); path.addLine(to: p) }
                        .stroke(Color(red: 0.55, green: 0.40, blue: 1.0).opacity(0.8), style: StrokeStyle(lineWidth: 16, lineCap: .round, dash: [26, 30]))
                    Circle().fill(Color(red: 0.06, green: 0.07, blue: 0.10)).frame(width: 150, height: 150).position(p)
                    Circle().stroke([Color(red: 0.20, green: 0.83, blue: 0.60), Color(red: 0.35, green: 0.55, blue: 1.0), Color(red: 0.95, green: 0.45, blue: 0.85)][i], lineWidth: 16)
                        .frame(width: 150, height: 150).position(p)
                }
                Circle().fill(Color(red: 0.55, green: 0.40, blue: 1.0).opacity(0.35)).frame(width: 170, height: 170).position(x: 400, y: 400)
                Circle().stroke(Color(red: 0.55, green: 0.40, blue: 1.0), lineWidth: 16).frame(width: 170, height: 170).position(x: 400, y: 400)
            }
            .frame(width: 800, height: 800).offset(y: 18).scaleEffect(1.12)
        }
        .frame(width: 824, height: 824)
        .shadow(color: .black.opacity(0.35), radius: 24, y: 12)
        .frame(width: 1024, height: 1024)
    }
}

@MainActor
func renderSwarmIcon(to path: String) {
    let r = ImageRenderer(content: SwarmIconView())
    r.scale = 1
    guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { print("icon render failed"); return }
    try? png.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}

// ---- snapshots for design review ----
@MainActor
func renderSwarmSnapshots(to dir: String) {
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    func shot(_ name: String, _ setup: (SwarmModel) -> Void) {
        let m = SwarmModel(); m.connected = true; setup(m)
        let r = ImageRenderer(content: SwarmWidget(model: m).background(Color(red: 0.13, green: 0.14, blue: 0.14)))
        r.scale = 2
        guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff), let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: URL(fileURLWithPath: "\(dir)/\(name).png")); print("wrote \(name).png")
    }
    let two = [SwarmDevice(name: "MacBook", status: "idle", online: true), SwarmDevice(name: "Mac mini", status: "idle", online: true)]
    shot("1_pill") { $0.devices = two }
    shot("2_open_idle") { $0.devices = two; $0.expanded = true }
    shot("2b_one_computer") { $0.devices = [two[1]]; $0.expanded = true }
    shot("2c_planning") { $0.devices = two; $0.expanded = true; $0.pending = [SwarmPending(text: "find me three ML roles and email me a summary", at: Date())] }
    shot("3_offline") { $0.devices = [two[0], SwarmDevice(name: "Mac mini", status: "offline", online: false)]; $0.expanded = true }
    shot("4_working") {
        $0.devices = [SwarmDevice(name: "MacBook", status: "busy", online: true), SwarmDevice(name: "Mac mini", status: "busy", online: true)]
        func sub(_ id: String, _ node: String, _ step: String) -> SwarmSub {
            SwarmSub(id: id, job: "j1", node: node, why: "you asked for all of them", state: "running", steps: [SwarmStep(text: step, done: false, failed: false)],
                     checklist: [SwarmCheck(text: "Open the careers page", status: "done"), SwarmCheck(text: "Find matching roles", status: "in_progress"), SwarmCheck(text: "Summarise the best three", status: "pending")], result: "", approval: "")
        }
        $0.jobs = [SwarmJob(id: "j1", request: "research Apple and Bloomberg ML roles on both", state: "running", result: "", subs: [sub("a", "MacBook", "Opening apple.com/careers"), sub("b", "Mac mini", "Opening bloomberg.com/careers")])]
    }
    shot("5_done") {
        $0.devices = two; $0.expanded = true
        func sub(_ id: String, _ node: String, _ r: String) -> SwarmSub { SwarmSub(id: id, job: "j2", node: node, why: "", state: "done", steps: [], checklist: [], result: r, approval: "") }
        $0.jobs = [SwarmJob(id: "j2", request: "research Apple and Bloomberg ML roles on both", state: "done",
                            result: "Apple has three ML roles in Services Engineering; Bloomberg has two in the AI group.",
                            subs: [sub("a", "MacBook", "Found 3 Apple roles."), sub("b", "Mac mini", "Found 2 Bloomberg roles.")])]
    }
}
