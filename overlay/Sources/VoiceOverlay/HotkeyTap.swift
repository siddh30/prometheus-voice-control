import Cocoa
import ApplicationServices

let keyCodes: [String: Int64] = ["f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
                                 "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "esc": 53]

/// Global hotkeys captured inside the app (which holds the Accessibility / Input Monitoring grants) and
/// forwarded to the daemon. Mode keys are swallowed so macOS never beeps or triggers media functions.
final class HotkeyTap {
    var send: (String) -> Void = { _ in }
    var isRecording: () -> Bool = { false }
    var onTrust: (Bool) -> Void = { _ in }

    private var modeKeys: [Int64: String] = [:]      // keycode -> key name (e.g. 101 -> "f9")
    private var cancelCode: Int64 = 53
    private var cancelName = "esc"
    private var held = Set<Int64>()                  // mode keys whose press had Control, so the release is ours even if Control came up first
    private var tap: CFMachPort?
    private var retry: Timer?

    func configure(hotkeys: [String], cancel: String) {
        modeKeys = Dictionary(uniqueKeysWithValues: hotkeys.compactMap { n in keyCodes[n].map { ($0, n) } })
        cancelCode = keyCodes[cancel] ?? 53
        cancelName = cancel
    }

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
        report("event tap installed")
        CFRunLoopAddSource(CFRunLoopGetMain(), CFMachPortCreateRunLoopSource(nil, t, 0), .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        return true
    }

    private func handle(_ type: CGEventType, _ event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return Unmanaged.passUnretained(event)
        }
        let code = event.getIntegerValueField(.keyboardEventKeycode)
        let isRepeat = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
        let down = type == .keyDown

        if let name = modeKeys[code] {
            // Mode keys are Control+key: a bare F9/F10 stays an ordinary key for every other app.
            if down {
                guard held.contains(code) || event.flags.contains(.maskControl) else { return Unmanaged.passUnretained(event) }
                held.insert(code)
            } else if held.remove(code) == nil {
                return Unmanaged.passUnretained(event)
            }
            if !isRepeat { if let j = Command.key(name: name, down: down).json { send(j) } }
            return nil
        }
        if code == cancelCode && isRecording() {         // Esc is only ours while a recording is active
            if down && !isRepeat { if let j = Command.key(name: cancelName, down: true).json { send(j) } }
            return nil
        }
        return Unmanaged.passUnretained(event)
    }
}
