import Cocoa
import ApplicationServices

let keyCodes: [String: Int64] = ["f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
                                 "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "esc": 53]

/// Global hotkeys captured inside the app (which holds the Accessibility / Input Monitoring grants) and
/// forwarded to the daemon. Mode keys are swallowed so macOS never beeps or triggers media functions.
///
/// The tap sits in front of EVERY keystroke on the Mac, so it lives on its own thread: if it ran on the widget's main thread, any moment that
/// thread was busy (drawing, waiting on the window server) would hold up the keyboard of the whole desktop.
final class HotkeyTap {
    var send: (String) -> Void = { _ in }              // called on the main thread
    var onTrust: (Bool) -> Void = { _ in }              // called on the main thread

    private let lock = NSLock()
    private var modeKeys: [Int64: String] = [:]      // keycode -> key name (e.g. 101 -> "f9")
    private var cancelCode: Int64 = 53
    private var cancelName = "esc"
    private var recording = false                       // Esc is ours only while a recording or conversation is active (set from the main thread)
    private var tap: CFMachPort?
    private var tapThread: Thread?
    private var retry: Timer?

    func configure(hotkeys: [String], cancel: String) {
        lock.lock(); defer { lock.unlock() }
        modeKeys = Dictionary(uniqueKeysWithValues: hotkeys.compactMap { n in keyCodes[n].map { ($0, n) } })
        cancelCode = keyCodes[cancel] ?? 53
        cancelName = cancel
    }

    func setRecording(_ on: Bool) { lock.lock(); recording = on; lock.unlock() }

    func start() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)          // shows the system prompt on first run
        if !CGPreflightListenEventAccess() { _ = CGRequestListenEventAccess() }
        attempt()
    }

    private func report(_ line: String) {
        let url = URL(fileURLWithPath: NSTemporaryDirectory() + "voicecontrol-permissions.log")
        let text = "\(Date()) \(line)\n"
        if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); h.write(text.data(using: .utf8)!); try? h.close() }
        else { try? text.write(to: url, atomically: true, encoding: .utf8) }
    }

    private func attempt() {
        report("accessibility=\(AXIsProcessTrusted()) inputMonitoring=\(CGPreflightListenEventAccess()) tapInstalled=\(tap != nil)")
        if tap == nil, install() {
            retry?.invalidate(); retry = nil
            onTrust(true)
            return
        }
        if tap == nil {
            onTrust(false)
            if retry == nil {
                retry = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.attempt() }
            }
        }
    }

    private func install() -> Bool {
        let mask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)
        let callback: CGEventTapCallBack = { _, type, event, refcon in
            guard let refcon else { return Unmanaged.passUnretained(event) }
            return Unmanaged<HotkeyTap>.fromOpaque(refcon).takeUnretainedValue().handle(type, event)
        }
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                        eventsOfInterest: CGEventMask(mask), callback: callback,
                                        userInfo: Unmanaged.passUnretained(self).toOpaque()) else { return false }
        tap = t
        report("event tap installed (own thread)")
        let source = CFMachPortCreateRunLoopSource(nil, t, 0)
        let thread = Thread {
            CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
            CGEvent.tapEnable(tap: t, enable: true)
            CFRunLoopRun()                                  // this thread does nothing but serve the keyboard hook
        }
        thread.name = "voicecontrol.hotkeys"
        thread.qualityOfService = .userInteractive
        thread.start()
        tapThread = thread
        return true
    }

    /// Hand the key to the app on the main thread; the tap itself returns at once.
    private func forward(_ json: String) { DispatchQueue.main.async { [weak self] in self?.send(json) } }

    private func handle(_ type: CGEventType, _ event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return Unmanaged.passUnretained(event)
        }
        let code = event.getIntegerValueField(.keyboardEventKeycode)
        let isRepeat = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
        let down = type == .keyDown

        lock.lock()
        let name = modeKeys[code], cancelHit = code == cancelCode && recording, cancel = cancelName
        lock.unlock()
        if let name {
            if !isRepeat { forward("{\"type\":\"key\",\"name\":\"\(name)\",\"down\":\(down)}") }
            return nil
        }
        if cancelHit {                                   // Esc is only ours while a recording is active
            if down && !isRepeat { forward("{\"type\":\"key\",\"name\":\"\(cancel)\",\"down\":true}") }
            return nil
        }
        return Unmanaged.passUnretained(event)
    }
}
