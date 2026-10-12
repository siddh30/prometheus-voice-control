import Cocoa
import ApplicationServices
import ServiceManagement

/// Prometheus Switch: a menu-bar app with one job. F12 quits whichever of Voice Control / Prometheus Swarm is running, waits until it has
/// really gone, and opens the other. (They share F9 and F10, so only one can run at a time.) It never force-quits: an app that will not
/// quit is reported, because killing it could leave its voice daemon running.
@MainActor
final class Switcher: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var tap: CFMachPort?
    private var retry: Timer?
    private var busy = false

    private var last: Mode? {
        get { UserDefaults.standard.string(forKey: "last").flatMap(Mode.init(rawValue:)) }
        set { UserDefaults.standard.set(newValue?.rawValue, forKey: "last") }
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "arrow.left.arrow.right.circle", accessibilityDescription: "Switch")
        item.button?.imagePosition = .imageLeft
        let nc = NSWorkspace.shared.notificationCenter
        nc.addObserver(self, selector: #selector(appsChanged), name: NSWorkspace.didLaunchApplicationNotification, object: nil)
        nc.addObserver(self, selector: #selector(appsChanged), name: NSWorkspace.didTerminateApplicationNotification, object: nil)
        refresh()
        startTap()
    }

    @objc private func appsChanged(_ n: Notification) { refresh() }

    // ---- what is running ----
    private func running() -> Set<Mode> {
        Set(Mode.allCases.filter { !NSRunningApplication.runningApplications(withBundleIdentifier: $0.bundleID).isEmpty })
    }

    private func refresh() {
        let r = running()
        if let m = r.first { last = m }
        item.button?.title = busy ? " …" : (r.first.map { " " + $0.label } ?? " –")
        item.button?.toolTip = "Press F12 to switch between Voice Control and Prometheus Swarm"
        item.menu = buildMenu(r)
    }

    private func buildMenu(_ r: Set<Mode>) -> NSMenu {
        let m = NSMenu()
        let status = NSMenuItem(title: r.isEmpty ? "Nothing is running" : "Running: " + r.map(\.appName).joined(separator: ", "), action: nil, keyEquivalent: "")
        status.isEnabled = false
        m.addItem(status)
        m.addItem(withTitle: "Switch now (F12)", action: #selector(switchNowMenu), keyEquivalent: "").target = self
        m.addItem(.separator())
        for mode in Mode.allCases {
            let i = m.addItem(withTitle: "Open " + mode.appName, action: #selector(openMode(_:)), keyEquivalent: "")
            i.target = self; i.representedObject = mode.rawValue; i.state = r.contains(mode) ? .on : .off
        }
        m.addItem(.separator())
        if tap == nil {
            m.addItem(withTitle: "F12 isn't active: allow keyboard access…", action: #selector(openPrivacy), keyEquivalent: "").target = self
        }
        let login = m.addItem(withTitle: "Open at login", action: #selector(toggleLogin), keyEquivalent: "")
        login.target = self; login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        m.addItem(.separator())
        m.addItem(withTitle: "Quit Prometheus Switch", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        return m
    }

    @objc private func switchNowMenu() { switchNow() }
    @objc private func openMode(_ sender: NSMenuItem) {
        if let raw = sender.representedObject as? String, let mode = Mode(rawValue: raw) { switchTo(mode) }
    }
    @objc private func openPrivacy() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")!)
    }
    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch { alert("Couldn't change the login setting", error.localizedDescription) }
        refresh()
    }

    // ---- switching ----
    func switchNow() { perform(plan(running: running(), last: last)) }

    private func switchTo(_ mode: Mode) {
        perform(Plan(quit: Array(running().filter { $0 != mode }), launch: mode))
    }

    private func perform(_ p: Plan) {
        guard !busy else { return }                                              // a second press during a switch does nothing
        busy = true
        refresh()
        for mode in p.quit {
            NSRunningApplication.runningApplications(withBundleIdentifier: mode.bundleID).forEach { $0.terminate() }
        }
        wait(until: p.quit, deadline: Date().addingTimeInterval(12)) { [weak self] stuck in
            guard let self else { return }
            if let stuck {
                self.busy = false; self.refresh()
                self.alert("\(stuck.appName) didn't quit", "Quit it yourself, then press F12 again. I don't force-quit it, because that could leave its voice daemon running.")
            } else {
                self.launch(p.launch)
            }
        }
    }

    /// Waits until every app in `modes` has really exited (its voice daemon stops with it), then calls back; `stuck` names one that did not.
    private func wait(until modes: [Mode], deadline: Date, _ done: @escaping (Mode?) -> Void) {
        let still = modes.first { !NSRunningApplication.runningApplications(withBundleIdentifier: $0.bundleID).isEmpty }
        if still == nil { done(nil); return }
        if Date() > deadline { done(still); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in self?.wait(until: modes, deadline: deadline, done) }
    }

    private func launch(_ mode: Mode) {
        let url = URL(fileURLWithPath: NSHomeDirectory() + "/Applications/\(mode.appName).app")
        guard FileManager.default.fileExists(atPath: url.path) else {
            busy = false; refresh()
            alert("\(mode.appName) isn't installed", "I looked for it at \(url.path).")
            return
        }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                self.last = mode
                self.refresh()
                if let error { self.alert("Couldn't open \(mode.appName)", error.localizedDescription) }
            }
        }
    }

    private func alert(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    // ---- the F12 key ----
    private func startTap() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)                                   // the system's one-time prompt
        if !CGPreflightListenEventAccess() { _ = CGRequestListenEventAccess() }
        attempt()
    }

    private func attempt() {
        guard tap == nil else { return }
        let mask: CGEventMask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)
        let me = Unmanaged.passUnretained(self).toOpaque()
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap, eventsOfInterest: mask,
                                        callback: { _, type, event, refcon in
            guard let refcon else { return Unmanaged.passUnretained(event) }
            return MainActor.assumeIsolated { Unmanaged<Switcher>.fromOpaque(refcon).takeUnretainedValue().handle(type, event) }
        }, userInfo: me) else {
            retry = Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in MainActor.assumeIsolated { self?.attempt() } }
            item.menu = buildMenu(running())                                      // shows "F12 isn't active" until the permission is given
            return
        }
        tap = t
        CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, t, 0), .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        item.menu = buildMenu(running())
    }

    /// Runs inside the keyboard event tap, so it only compares a key code and hands the real work to the main queue.
    private func handle(_ type: CGEventType, _ event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let t = tap { CGEvent.tapEnable(tap: t, enable: true) }
            return Unmanaged.passUnretained(event)
        }
        let mods: CGEventFlags = [.maskCommand, .maskControl, .maskAlternate]
        guard event.getIntegerValueField(.keyboardEventKeycode) == 111, event.flags.intersection(mods).isEmpty else {   // F12, no modifiers
            return Unmanaged.passUnretained(event)
        }
        if type == .keyDown && event.getIntegerValueField(.keyboardEventAutorepeat) == 0 {
            DispatchQueue.main.async { [weak self] in MainActor.assumeIsolated { self?.switchNow() } }
        }
        return nil                                                                // swallowed: macOS must not also treat F12 as volume up
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)                                          // menu bar only: no Dock icon
    let delegate = Switcher()
    app.delegate = delegate
    app.run()
}
