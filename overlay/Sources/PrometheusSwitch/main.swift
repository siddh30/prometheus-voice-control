import Cocoa
import ApplicationServices
import ServiceManagement
import SwiftUI

/// Prometheus Switch: a menu-bar app with one job. While it is running, F12 (anywhere) or the button on its card quits whichever of Voice
/// Control / Prometheus Swarm is running, waits until it has really gone, and opens the other. (They share F9 and F10, so only one can run
/// at a time.) The card extends down from the menu-bar icon as feedback and disappears when it is done. Until the one-time keyboard
/// permissions are granted, F12 still works while the card is open. It never force-quits: an app that will not quit is reported, because
/// killing it could leave its voice daemon running.

/// ~/Library/Logs/PrometheusSwitch.log: every key press, click, decision and outcome, so "it did nothing" can always be explained.
func note(_ text: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(text)\n"
    let url = URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/PrometheusSwitch.log")
    if let handle = try? FileHandle(forWritingTo: url) { handle.seekToEndOfFile(); handle.write(Data(line.utf8)); try? handle.close() }
    else { try? line.write(to: url, atomically: true, encoding: .utf8) }
    print(line, terminator: "")
}

/// The floating card's window. It exists on screen only while the card is open, and then it takes the keyboard (so F12 works).
final class WidgetPanel: NSPanel {
    var wantsKey = false
    override var canBecomeKey: Bool { wantsKey }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class Switcher: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var item: NSStatusItem?
    private var widget: WidgetPanel?
    private var collapseTimer: Timer?
    private var expandedAt = Date.distantPast
    private let model = SwitchModel()
    private var keyMonitor: Any?
    private var tap: CFMachPort?
    private var retry: Timer?
    private var watchdog: Timer?
    private var activity: NSObjectProtocol?                                     // keeps macOS from napping this window-less app
    private var busy = false

    private var last: Mode? {
        get { UserDefaults.standard.string(forKey: "last").flatMap(Mode.init(rawValue:)) }
        set { UserDefaults.standard.set(newValue?.rawValue, forKey: "last") }
    }

    // ---- start ----
    func applicationDidFinishLaunching(_ n: Notification) {
        if let i = CommandLine.arguments.firstIndex(of: "--snapshot"), i + 1 < CommandLine.arguments.count {
            renderSnapshots(to: CommandLine.arguments[i + 1]); exit(0)
        }
        if let i = CommandLine.arguments.firstIndex(of: "--now") { runOnce(CommandLine.arguments.dropFirst(i + 1).first); return }
        note("started (pid \(ProcessInfo.processInfo.processIdentifier))")
        // A menu-bar app with no window is what macOS "App Nap" throttles, which would make F12 unreliable when the card is closed.
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep, .latencyCritical],
                                                         reason: "Listening for the F12 key")
        model.onPick = { [weak self] mode in self?.switchTo(mode) }
        model.onPrivacy = {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")!)
        }
        model.onLogin = { [weak self] in self?.toggleLogin() }
        model.onQuit = { note("Quit pressed on the card"); NSApp.terminate(nil) }
        model.onCollapse = { [weak self] in self?.setExpanded(false) }
        makeWidget()

        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        status.button?.image = NSImage(systemSymbolName: "arrow.left.arrow.right.circle", accessibilityDescription: "Switch")
        status.button?.imagePosition = .imageLeft
        status.button?.target = self
        status.button?.action = #selector(statusClicked(_:))
        status.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        item = status

        let nc = NSWorkspace.shared.notificationCenter
        nc.addObserver(self, selector: #selector(appsChanged), name: NSWorkspace.didLaunchApplicationNotification, object: nil)
        nc.addObserver(self, selector: #selector(appsChanged), name: NSWorkspace.didTerminateApplicationNotification, object: nil)
        refresh()
        startTap()
        // macOS switches a key listener off if it is ever slow to answer; check often and switch it back on.
        watchdog = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, let t = self.tap, !CGEvent.tapIsEnabled(tap: t) else { return }
                CGEvent.tapEnable(tap: t, enable: true)
                note("F12 tap had been switched off by macOS: switched back on")
            }
        }
        // Fallback for before the permissions are granted: F12 is an ordinary key event for the card while the card is open.
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            return MainActor.assumeIsolated {
                guard self.model.expanded, event.keyCode == 111, event.modifierFlags.intersection([.command, .control, .option]).isEmpty else { return event }
                if !event.isARepeat { note("F12 pressed (window open)"); self.switchNow() }
                return nil                                                       // swallowed, so macOS does not also change the volume
            }
        }
        if launchedAtLogin { note("started at login: nothing on screen but the menu-bar icon") }
        else {                                                                    // you opened the app: the card floats open under the icon, then goes away
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.setExpanded(true); self?.armCollapse(after: 6) }
        }
    }

    func applicationWillTerminate(_ n: Notification) { note("terminating (quit from the card or the menu, or macOS asked it to)") }

    /// Started by "Open at login" (as opposed to you opening it): no window should jump up at every login.
    private var launchedAtLogin: Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent, event.eventID == AEEventID(kAEOpenApplication) else { return false }
        return event.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue == AEKeyword(keyAELaunchedAsLogInItem)
    }

    // ---- the floating card: hidden until you click the menu-bar icon ----
    private func makeWidget() {
        let panel = WidgetPanel(contentRect: NSRect(origin: .zero, size: WidgetSize.card), styleMask: [.borderless, .nonactivatingPanel],
                                backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isMovableByWindowBackground = true                                   // drag it wherever you like while it is open
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.delegate = self
        let hosting = NSHostingView(rootView: SwitchWidget(model: model))
        hosting.frame = NSRect(origin: .zero, size: WidgetSize.card)
        panel.contentView = hosting
        widget = panel                                                             // not shown yet
    }

    /// Show the card (floating just under the menu-bar icon, taking the keyboard so F12 works), or hide it completely.
    private func setExpanded(_ on: Bool) {
        guard let panel = widget else { return }
        collapseTimer?.invalidate()
        model.expanded = on
        panel.wantsKey = on
        if on {
            expandedAt = Date()
            let card = WidgetSize.card
            let screen = (panel.screen ?? NSScreen.main ?? NSScreen.screens[0]).visibleFrame
            if let icon = item?.button?.window?.frame {                            // right under the icon, kept on screen
                let x = min(max(icon.maxX - card.width, screen.minX + 8), screen.maxX - card.width - 8)
                panel.setFrame(NSRect(x: x, y: icon.minY - card.height - 6, width: card.width, height: card.height), display: true)
            }
            panel.makeKeyAndOrderFront(nil)
            armCollapse()
        } else {
            panel.orderOut(nil)
        }
        refresh()
    }

    /// An open card with nobody using it disappears.
    private func armCollapse(after seconds: TimeInterval = 12) {
        collapseTimer?.invalidate()
        collapseTimer = Timer.scheduledTimer(withTimeInterval: seconds, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { if self?.busy == false { self?.setExpanded(false) } }
        }
    }

    func windowDidResignKey(_ notification: Notification) {                      // you clicked somewhere else: the card disappears
        guard Date().timeIntervalSince(expandedAt) > 2.0 else { return }          // not while macOS is still shuffling focus around the opening
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            MainActor.assumeIsolated { if self?.model.expanded == true, self?.busy == false { self?.setExpanded(false) } }
        }
    }

    /// Opening the app again (Finder, Spotlight, Dock) shows the panel.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        note("reopened")
        setExpanded(true)
        return true
    }

    @objc private func appsChanged(_ n: Notification) { refresh() }

    // ---- the menu-bar icon: a way back to the widget ----
    @objc private func statusClicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "Quit Prometheus Switch", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            item?.menu = menu
            item?.button?.performClick(nil)
            item?.menu = nil
        } else {
            note("icon clicked")
            setExpanded(!model.expanded)
        }
    }

    // ---- what is running ----
    private func running() -> Set<Mode> {
        Set(Mode.allCases.filter { !NSRunningApplication.runningApplications(withBundleIdentifier: $0.bundleID).isEmpty })
    }

    private func refresh() {
        let r = running()
        if let m = r.first { last = m }
        model.running = r
        model.keyActive = tap != nil
        model.loginOn = SMAppService.mainApp.status == .enabled
        item?.button?.title = busy ? " …" : (r.first.map { " " + $0.label } ?? " –")
        item?.button?.toolTip = "Click to open the switcher"
    }

    private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() } else { try SMAppService.mainApp.register() }
        } catch { problem("Couldn't change the login setting: \(error.localizedDescription)") }
        refresh()
    }

    // ---- switching ----
    func switchNow() {
        note("switch requested (running: \(running().map(\.rawValue).sorted()), last: \(last?.rawValue ?? "none"))")
        perform(plan(running: running(), last: last))
    }

    private func switchTo(_ mode: Mode) {
        note("panel: switch to \(mode.appName)")
        perform(Plan(quit: running().filter { $0 != mode }.map { $0 }, launch: mode))
    }

    private func problem(_ text: String) {
        note("problem: \(text)")
        model.message = text
        model.failed = true
        setExpanded(true)
    }

    private func perform(_ p: Plan, then finished: ((String?) -> Void)? = nil) {
        guard !busy else { note("ignored: a switch is already in progress"); finished?("busy"); return }     // a second press does nothing
        note("plan: quit \(p.quit.map(\.rawValue)), then open \(p.launch.rawValue)")
        busy = true
        model.busy = true
        model.failed = false
        model.message = p.quit.isEmpty ? "Opening \(p.launch.appName)…" : "Closing \(p.quit.map(\.appName).joined(separator: " and "))…"
        refresh()
        setExpanded(true)
        for mode in p.quit {
            NSRunningApplication.runningApplications(withBundleIdentifier: mode.bundleID).forEach { $0.terminate() }
        }
        wait(until: p.quit, deadline: Date().addingTimeInterval(12)) { [weak self] stuck in
            guard let self else { return }
            if let stuck {
                note("\(stuck.appName) did not quit within 12 s")
                self.busy = false; self.model.busy = false; self.refresh()
                finished?("\(stuck.appName) did not quit")
                self.problem("\(stuck.appName) didn't quit. Quit it yourself, then try again. I don't force-quit it, because that could leave its voice daemon running.")
            } else {
                self.model.message = "Opening \(p.launch.appName)…"
                self.launch(p.launch, finished)
            }
        }
    }

    /// `PrometheusSwitch --now [single|swarm]`: do one switch without any UI, print what happened, exit. (Also how this app is tested.)
    private func runOnce(_ target: String?) {
        let p: Plan
        if let t = target.flatMap(Mode.init(rawValue:)) { p = Plan(quit: running().filter { $0 != t }.map { $0 }, launch: t) }
        else { p = plan(running: running(), last: last) }
        perform(p) { error in
            note(error == nil ? "once: done" : "once: FAILED: \(error!)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exit(error == nil ? 0 : 1) }
        }
    }

    /// Waits until every app in `modes` has really exited (its voice daemon stops with it), then calls back; `stuck` names one that did not.
    private func wait(until modes: [Mode], deadline: Date, _ done: @escaping (Mode?) -> Void) {
        let still = modes.first { !NSRunningApplication.runningApplications(withBundleIdentifier: $0.bundleID).isEmpty }
        if still == nil { done(nil); return }
        if Date() > deadline { done(still); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in self?.wait(until: modes, deadline: deadline, done) }
    }

    private func launch(_ mode: Mode, _ finished: ((String?) -> Void)? = nil) {
        let url = URL(fileURLWithPath: NSHomeDirectory() + "/Applications/\(mode.appName).app")
        note("opening \(url.path)")
        guard FileManager.default.fileExists(atPath: url.path) else {
            busy = false; model.busy = false; refresh()
            finished?("not installed")
            problem("\(mode.appName) isn't installed. I looked for it at \(url.path).")
            return
        }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                self.model.busy = false
                self.last = mode
                self.refresh()
                note(error == nil ? "opened \(mode.appName)" : "could not open \(mode.appName): \(error!.localizedDescription)")
                finished?(error?.localizedDescription)
                if let error { self.problem("Couldn't open \(mode.appName): \(error.localizedDescription)") }
                else { self.model.message = "\(mode.appName) is open"; self.model.failed = false; self.armCollapse(after: 2.5) }
            }
        }
    }

    // ---- the F12 key, anywhere ----
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
            note("F12 tap could not be created: Accessibility / Input Monitoring not granted yet (retrying every 2 s); F12 works while the card is open")
            model.keyActive = false
            retry = Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in MainActor.assumeIsolated { self?.attempt() } }
            return
        }
        tap = t
        CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, t, 0), .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        model.keyActive = true
        note("F12 tap is active: F12 works anywhere")
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
            note("F12 pressed")
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
