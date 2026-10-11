import AppKit
import SwiftUI
import Combine
import Combine

/// Borderless, non-activating panel so the overlay never steals focus from the app you are typing into.
final class OverlayPanel: NSPanel {
    /// Never key (so it can't steal typing from the app you're in), except while you are typing a message to Prometheus.
    var allowKey = false
    override var canBecomeKey: Bool { allowKey }
    override var canBecomeMain: Bool { false }

    /// An app without a menu bar has no Edit menu, so cmd+V/C/X/A do nothing in its text fields. Route them ourselves.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard event.type == .keyDown, event.modifierFlags.intersection([.command, .option, .control]) == .command,
              let key = event.charactersIgnoringModifiers?.lowercased() else { return super.performKeyEquivalent(with: event) }
        let action: Selector?
        switch key {
        case "v": action = #selector(NSText.paste(_:))
        case "c": action = #selector(NSText.copy(_:))
        case "x": action = #selector(NSText.cut(_:))
        case "a": action = #selector(NSText.selectAll(_:))
        case "z": action = Selector(("undo:"))
        default: action = nil
        }
        if let action, NSApp.sendAction(action, to: nil, from: self) { return true }
        return super.performKeyEquivalent(with: event)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: OverlayPanel!
    var socket: DaemonSocket!
    let model = OverlayModel()
    var approvalWatch: AnyCancellable?
    var supervisor: DaemonSupervisor?
    let hotkeys = HotkeyTap()
    var badgeWatch: AnyCancellable?
    var typingWatch: AnyCancellable?
    var bag = Set<AnyCancellable>()
    var swarmSocket: SwarmSocket?
    var hosting: NSHostingView<OverlayView>?
    var fitWatch: AnyCancellable?
    var fitTimer: Timer?
    var recordingWatch: AnyCancellable?
    var swarmWatch: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        supervisor = DaemonSupervisor()
        supervisor?.start()
        let host = ProcessInfo.processInfo.environment["VOICECTL_WS"] ?? "ws://127.0.0.1:8765"
        socket = DaemonSocket(url: URL(string: host)!, model: model)
        hotkeys.send = { [weak self] msg in self?.socket.send(msg) }
        hotkeys.onTrust = { [weak self] ok in MainActor.assumeIsolated { self?.model.needsPermission = !ok } }
        socket.onConfig = { [weak self] keys, cancel in self?.hotkeys.configure(hotkeys: keys, cancel: cancel) }
        hotkeys.start()
        model.linkSwarm()
        startSwarmLink()
        // Esc belongs to us only while a recording or conversation is active; the keyboard hook (its own thread) reads this flag, never the UI.
        func syncRecordingFlag() {
            let on = model.conversational ? (model.phase != .idle && model.phase != .done) : model.phase == .listening
            hotkeys.setRecording(on)
        }
        recordingWatch = model.$phase.combineLatest(model.$mode).receive(on: RunLoop.main).sink { _ in syncRecordingFlag() }
        // Dock icon badge: you can see "REC" even when the widget is hidden behind a full-screen app.
        badgeWatch = model.$phase.receive(on: RunLoop.main).sink { phase in
            switch phase {
            case .listening: NSApp.dockTile.badgeLabel = "REC"
            case .transcribing, .handling, .thinking, .acting: NSApp.dockTile.badgeLabel = "…"
            case .awaiting_approval: NSApp.dockTile.badgeLabel = "?"
            default: NSApp.dockTile.badgeLabel = nil
            }
        }

        let hosting = NSHostingView(rootView: OverlayView(model: model, onSize: { [weak self] size in
            DispatchQueue.main.async { if size.width > 1 { self?.sizeSettled(size) } }
        }))
        self.hosting = hosting
        // The app sizes the window itself (place(size:)). Left to SwiftUI, the hosting view re-negotiates the window's size limits with the
        // window server on every animation frame, which blocks this app for half of its time and stalls the whole desktop while it animates.
        if #available(macOS 13.0, *) { hosting.sizingOptions = [.intrinsicContentSize] }      // reports its size; no min/max window-limit negotiation
        panel = OverlayPanel(contentRect: NSRect(x: 0, y: 0, width: 200, height: 80),
                             styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.contentView = hosting
        typingWatch = model.$typing.sink { [weak self] on in
            guard let self = self else { return }
            self.panel.allowKey = on
            if on { self.panel.makeKey() } else { self.panel.resignKey() }
        }
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .statusBar                       // floats above normal windows, incl. full-screen apps
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isMovableByWindowBackground = true       // drag it anywhere
        loadAnchor()
        adaptToScreen()
        place(size: CGSize(width: 260, height: 60))       // until the widget reports its real size
        // The window follows the widget's real size. SwiftUI's own size report proved unreliable inside this window (it said 0 x 0), so the
        // hosting view is asked directly whenever anything changes, and once a second as a safety net.
        fitWatch = model.objectWillChange.merge(with: model.swarm.objectWillChange).debounce(for: .milliseconds(60), scheduler: RunLoop.main)
            .sink { [weak self] _ in self?.refit() }
        fitTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in MainActor.assumeIsolated { self?.refit() } }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in self?.refit() }
        panel.orderFrontRegardless()

        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(userMoved),
                                               name: NSWindow.didMoveNotification, object: panel)
    }

    // The window always hugs the widget. Position is stored as an anchor (center-x + the edge that stays
    // put), so the widget grows downward when it sits in the top half of the screen and upward otherwise.
    var anchorX: CGFloat = 0, anchorY: CGFloat = 0, growsDown = false
    var placing = false

    func defaultAnchor() {
        guard let vf = NSScreen.main?.visibleFrame else { return }
        anchorX = vf.midX; anchorY = vf.minY + 12; growsDown = false
    }

    func loadAnchor() {
        let d = UserDefaults.standard
        if d.object(forKey: "anchorX") != nil {
            anchorX = d.double(forKey: "anchorX"); anchorY = d.double(forKey: "anchorY"); growsDown = d.bool(forKey: "growsDown")
        } else { defaultAnchor() }
    }

    func refit() {
        guard let hosting else { return }
        let size = hosting.fittingSize
        if size.width > 1, size.height > 1 { sizeSettled(size) }
    }

    private var pendingSize: CGSize?
    private var sizeTimer: Timer?
    /// The widget reports its size on every layout pass (including during its own animations). Resizing a window costs the window server real
    /// work, so only the size it settles on is applied: tiny changes are ignored and bursts are merged into one resize.
    func sizeSettled(_ size: CGSize) {
        if ProcessInfo.processInfo.environment["VOICECTL_DEBUG_SIZE"] != nil { FileHandle.standardError.write("size reported: \(size) current: \(panel.frame.size)\n".data(using: .utf8)!) }
        let current = panel.frame.size
        if abs(size.width - current.width) < 2 && abs(size.height - current.height) < 2 { return }
        pendingSize = size
        sizeTimer?.invalidate()
        sizeTimer = Timer.scheduledTimer(withTimeInterval: 0.07, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { if let s = self?.pendingSize { self?.pendingSize = nil; self?.place(size: s) } }
        }
    }

    func place(size: CGSize) {
        guard size.width > 1, size.height > 1 else { return }
        let screen = NSScreen.screens.first { $0.frame.contains(NSPoint(x: anchorX, y: anchorY)) } ?? NSScreen.main
        guard let vf = screen?.visibleFrame else { return }
        var x = anchorX - size.width / 2
        var y = growsDown ? anchorY - size.height : anchorY
        x = min(max(x, vf.minX), vf.maxX - size.width)
        y = max(min(y, vf.maxY - size.height), vf.minY)   // on a short screen keep the bottom (where the widget sits) visible
        placing = true
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
        placing = false
    }

    @objc func userMoved() {
        guard !placing, let vf = panel.screen?.visibleFrame else { return }
        let f = panel.frame
        growsDown = f.midY > vf.midY
        anchorX = f.midX
        anchorY = growsDown ? f.maxY : f.minY
        let d = UserDefaults.standard
        d.set(anchorX, forKey: "anchorX"); d.set(anchorY, forKey: "anchorY"); d.set(growsDown, forKey: "growsDown")
    }

    func resetPosition() {
        UserDefaults.standard.removeObject(forKey: "anchorX")
        defaultAnchor()
        place(size: panel.frame.size)
    }

    /// The open card is about a third of a laptop screen and a bit narrower on a big monitor: readable everywhere, never wider than 520 pt.
    func adaptToScreen() {
        let screen = NSScreen.screens.first { $0.frame.contains(NSPoint(x: anchorX, y: anchorY)) } ?? NSScreen.main
        guard let w = screen?.visibleFrame.width else { return }
        let target = min(520, max(400, w * 0.32))
        if abs(model.cardWidth - target) > 1 { model.cardWidth = target }
    }

    @objc func screensChanged() { adaptToScreen(); place(size: panel.frame.size) }

    func applicationWillTerminate(_ n: Notification) { supervisor?.stop() }

    // Right-click the Dock icon for these.
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let m = NSMenu()
        m.addItem(withTitle: "Open Phoenix traces", action: #selector(openPhoenix), keyEquivalent: "").target = self
        m.addItem(withTitle: model.calm ? "Calm mode: on (click to bring animation back)" : "Calm mode: off (click to stop all animation)", action: #selector(toggleCalm), keyEquivalent: "").target = self
        m.addItem(withTitle: "Restart voice daemon", action: #selector(restartDaemon), keyEquivalent: "").target = self
        m.addItem(withTitle: wakePaused ? "Resume wake word" : "Pause wake word", action: #selector(toggleWake), keyEquivalent: "").target = self
        m.addItem(withTitle: model.voiceOn ? "Voice replies: on (click for text only)" : "Text only (click to hear replies)", action: #selector(toggleVoiceMenu), keyEquivalent: "").target = self
        m.addItem(withTitle: model.swarmMode ? "Swarm mode: on (click for single)" : "Swarm mode: off (click to use all my computers)", action: #selector(toggleSwarmMenu), keyEquivalent: "").target = self
        if FileManager.default.fileExists(atPath: (supervisor?.dataDir.appendingPathComponent("swarm_url.txt").path) ?? "") {
            m.addItem(withTitle: "Open the swarm dashboard in a browser", action: #selector(openSwarmMenu), keyEquivalent: "").target = self
        }
        m.addItem(withTitle: "New conversation (clean context)", action: #selector(newChatMenu), keyEquivalent: "").target = self
        m.addItem(withTitle: "Start fresh (forget everything)", action: #selector(startFreshMenu), keyEquivalent: "").target = self
        m.addItem(withTitle: "Clean memory now", action: #selector(cleanMemory), keyEquivalent: "").target = self
        m.addItem(withTitle: "Reset widget position", action: #selector(resetPos), keyEquivalent: "").target = self
        return m
    }
    @objc func toggleCalm() { model.calm.toggle() }
    @objc func openPhoenix() { NSWorkspace.shared.open(URL(string: "http://localhost:6006")!) }
    var wakePaused = false
    @objc func toggleWake() {
        wakePaused.toggle()
        socket.send("{\"type\":\"wake\",\"enabled\":\(!wakePaused)}")
    }
    @objc func toggleVoiceMenu() { model.toggleVoice() }
    @objc func openSwarmMenu() { supervisor?.openSwarm() }
    @objc func toggleSwarmMenu() { model.toggleSwarmMode() }
    @objc func newChatMenu() { model.newConversation() }
    @objc func startFreshMenu() { model.startFresh() }
    @objc func cleanMemory() { socket.send("{\"type\":\"maintenance\"}") }
    @objc func resetPos() { resetPosition() }
    @objc func restartDaemon() { supervisor?.restartDaemon() }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }


    /// The link to the swarm hub (for the devices map in Swarm mode). It starts as soon as this computer knows where the hub is.
    func startSwarmLink() {
        if swarmSocket == nil, let raw = ProcessInfo.processInfo.environment["VOICECTL_SWARM_URL"], let url = URL(string: raw) {     // development: point at a hub directly
            let link = SwarmSocket(model: model.swarm, urlFile: URL(fileURLWithPath: "/dev/null"), override: url)
            swarmSocket = link; link.start(); return
        }
        guard swarmSocket == nil, let dir = supervisor?.dataDir else { return }
        let file = dir.appendingPathComponent("swarm_url.txt")
        guard FileManager.default.fileExists(atPath: file.path) else {
            if swarmWatch == nil {                              // the daemon writes it a moment after it starts
                swarmWatch = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                    MainActor.assumeIsolated { self?.startSwarmLink(); if self?.swarmSocket != nil { self?.swarmWatch?.invalidate(); self?.swarmWatch = nil } }
                }
            }
            return
        }
        let link = SwarmSocket(model: model.swarm, urlFile: file)
        swarmSocket = link
        link.start()
    }

    /// Bottom-center of the main screen, above the Dock.
    func position() {
        guard let screen = NSScreen.main else { return }
        let f = screen.visibleFrame
        panel.setFrame(NSRect(x: f.midX - panel.frame.width / 2, y: f.minY + 12, width: panel.frame.width, height: panel.frame.height), display: true)
    }
}

MainActor.assumeIsolated {
    if let i = CommandLine.arguments.firstIndex(of: "--icon"), i + 1 < CommandLine.arguments.count {
        _ = NSApplication.shared
        renderIcon(to: CommandLine.arguments[i + 1])
        exit(0)
    }
    if let i = CommandLine.arguments.firstIndex(of: "--snapshot"), i + 1 < CommandLine.arguments.count {
        _ = NSApplication.shared
        renderSnapshots(to: CommandLine.arguments[i + 1])
        exit(0)
    }
    let app = NSApplication.shared
    // Inside the .app: normal Dock icon. From the command line: stay invisible (dev mode).
    app.setActivationPolicy(Bundle.main.bundlePath.hasSuffix(".app") ? .regular : .accessory)
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
