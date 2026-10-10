import AppKit

/// Types text into the focused app: set clipboard, send Cmd+V, restore the previous text clipboard.
enum Paster {
    static func paste(_ text: String, restore: Bool = true) {
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
}
