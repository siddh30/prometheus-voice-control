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

    func applicationDidFinishLaunching(_ n: Notification) {
        supervisor = DaemonSupervisor()
        supervisor?.start()
        let host = ProcessInfo.processInfo.environment["VOICECTL_WS"] ?? "ws://127.0.0.1:8765"
        socket = DaemonSocket(url: URL(string: host)!, model: model)
        hotkeys.send = { [weak self] msg in self?.socket.send(msg) }
        hotkeys.isRecording = { MainActor.assumeIsolated { [weak self] in
            guard let m = self?.model else { return false }
            // Esc is ours while recording, and for the whole span of a hands-free conversation
            if m.conversational { return m.phase != .idle && m.phase != .done }
            return m.phase == .listening
        } }
        hotkeys.onTrust = { [weak self] ok in MainActor.assumeIsolated { self?.model.needsPermission = !ok } }
        socket.onConfig = { [weak self] keys, cancel in self?.hotkeys.configure(hotkeys: keys, cancel: cancel) }
        hotkeys.start()
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
            DispatchQueue.main.async { self?.place(size: size) }
        }))
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
        place(size: CGSize(width: 480, height: 640))   // the OverlayView's fixed frame
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

    @objc func screensChanged() { place(size: panel.frame.size) }

    func applicationWillTerminate(_ n: Notification) { supervisor?.stop() }

    // Right-click the Dock icon for these.
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let m = NSMenu()
        m.addItem(withTitle: "Open Phoenix traces", action: #selector(openPhoenix), keyEquivalent: "").target = self
        m.addItem(withTitle: "Restart voice daemon", action: #selector(restartDaemon), keyEquivalent: "").target = self
        m.addItem(withTitle: wakePaused ? "Resume wake word" : "Pause wake word", action: #selector(toggleWake), keyEquivalent: "").target = self
        m.addItem(withTitle: model.voiceOn ? "Voice replies: on (click for text only)" : "Text only (click to hear replies)", action: #selector(toggleVoiceMenu), keyEquivalent: "").target = self
        m.addItem(withTitle: "New conversation (clean context)", action: #selector(newChatMenu), keyEquivalent: "").target = self
        m.addItem(withTitle: "Start fresh (forget everything)", action: #selector(startFreshMenu), keyEquivalent: "").target = self
        m.addItem(withTitle: "Clean memory now", action: #selector(cleanMemory), keyEquivalent: "").target = self
        m.addItem(withTitle: "Reset widget position", action: #selector(resetPos), keyEquivalent: "").target = self
        return m
    }
    @objc func openPhoenix() { NSWorkspace.shared.open(URL(string: "http://localhost:6006")!) }
    var wakePaused = false
    @objc func toggleWake() {
        wakePaused.toggle()
        if let j = Command.wake(enabled: !wakePaused).json { socket.send(j) }
    }
    @objc func toggleVoiceMenu() { model.toggleVoice() }
    @objc func newChatMenu() { model.newConversation() }
    @objc func startFreshMenu() { model.startFresh() }
    @objc func cleanMemory() { if let j = Command.maintenance.json { socket.send(j) } }
    @objc func resetPos() { resetPosition() }
    @objc func restartDaemon() { supervisor?.restartDaemon() }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }


    /// Bottom-center of the main screen, above the Dock.
    func position() {
        guard let screen = NSScreen.main else { return }
        let f = screen.visibleFrame
        panel.setFrame(NSRect(x: f.midX - 240, y: f.minY + 12, width: 480, height: 640), display: true)
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
