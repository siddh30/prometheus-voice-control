import AppKit

/// Types text into the focused app: set clipboard, send Cmd+V, restore the previous text clipboard.
/// With Citrix mode on (the toggle on the widget) it does not touch the clipboard: it presses the keys one by one, as the
/// keyboard does, which is what a remote-desktop window accepts.
enum Paster {
    static let citrixKey = "citrixMode"
    static var citrixMode: Bool {
        get { UserDefaults.standard.bool(forKey: citrixKey) }
        set { UserDefaults.standard.set(newValue, forKey: citrixKey) }
    }

    private static let typingQueue = DispatchQueue(label: "voicectl.typing")

    static func paste(_ text: String, restore: Bool = true) {
        if citrixMode { return typeKeys(text) }
        let pb = NSPasteboard.general
        let previous = pb.string(forType: .string)
        pb.clearContents()
        pb.setString(text, forType: .string)

        let src = CGEventSource(stateID: .hidSystemState)
        let down = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: true)   // 9 = "v"
        let up = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: false)
        down?.flags = .maskCommand
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)

        if restore, let previous {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                pb.clearContents()
                pb.setString(previous, forType: .string)
            }
        }
    }

    // US-layout key codes: unshifted character -> key code; shifted symbols map to their unshifted key.
    private static let codes: [Character: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14,
        "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27,
        "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
        "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50, " ": 49, "\n": 36, "\t": 48,
    ]
    private static let shifted: [Character: Character] = [
        "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
        "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`",
    ]

    private static let shiftKey: CGKeyCode = 56

    /// Milliseconds between key events. A remote session drops or reorders keys sent faster than its link carries them
    /// (missing spaces, misspelt words), so the default is unhurried; tune with `defaults write <bundle id> citrixKeyDelayMs 25`.
    private static var keyDelay: useconds_t {
        let ms = UserDefaults.standard.integer(forKey: "citrixKeyDelayMs")
        return useconds_t((ms > 0 ? ms : 12) * 1_000)
    }

    private static func typeKeys(_ text: String) {
        typingQueue.async {
            let src = CGEventSource(stateID: .privateState)   // not the keys you still hold (Control from the hotkey)
            let wait = keyDelay
            func post(_ code: CGKeyCode, _ down: Bool, _ flags: CGEventFlags) {
                let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
                e?.flags = flags
                e?.post(tap: .cghidEventTap)
                usleep(wait)
            }
            for ch in text {
                var base = ch
                var shift = false
                if let s = shifted[ch] { base = s; shift = true }
                else if ch.isUppercase, ch.isASCII { base = Character(ch.lowercased()); shift = true }
                guard let code = codes[base] else {            // not on a US keyboard (accents, symbols): send it as text
                    for down in [true, false] {
                        let e = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: down)
                        e?.flags = []
                        var u = Array(String(ch).utf16)
                        e?.keyboardSetUnicodeString(stringLength: u.count, unicodeString: &u)
                        e?.post(tap: .cghidEventTap)
                        usleep(wait)
                    }
                    continue
                }
                // A real Shift press around the key: a remote session follows the Shift key, not just the flag on the letter.
                let flags: CGEventFlags = shift ? .maskShift : []
                if shift { post(shiftKey, true, .maskShift) }
                post(code, true, flags)
                post(code, false, flags)
                if shift { post(shiftKey, false, []) }
            }
        }
    }
}
