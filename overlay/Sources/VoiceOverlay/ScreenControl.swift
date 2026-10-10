import AppKit
import CoreGraphics

/// Eyes and hands for the agent. Runs inside the app because the app is what macOS has granted
/// Screen Recording and Accessibility to; the Python daemon asks for each operation over the WebSocket.
enum ScreenControl {
    // MARK: dispatch
    static func handle(_ req: [String: Any]) -> [String: Any] {
        let op = (req["op"] as? String) ?? ""
        func num(_ k: String) -> CGFloat { CGFloat((req[k] as? Double) ?? Double((req[k] as? Int) ?? 0)) }
        switch op {
        case "screenshot":
            var mark: CGPoint?
            if req["markX"] != nil { mark = CGPoint(x: num("markX"), y: num("markY")) }
            return screenshot(target: (req["target"] as? String) ?? "chrome", mark: mark)
        case "click":
            guard AXIsProcessTrusted() else { return noAccessibility }
            focusChrome()                      // clicks and scrolls must land in Chrome, not in whatever app was in front
            click(CGPoint(x: num("x"), y: num("y")), button: (req["button"] as? String) ?? "left", count: (req["count"] as? Int) ?? 1)
            return ["ok": true]
        case "move":
            guard AXIsProcessTrusted() else { return noAccessibility }
            focusChrome()                      // clicks and scrolls must land in Chrome, not in whatever app was in front
            post(.mouseMoved, CGPoint(x: num("x"), y: num("y")))
            return ["ok": true]
        case "drag":
            guard AXIsProcessTrusted() else { return noAccessibility }
            focusChrome()                      // clicks and scrolls must land in Chrome, not in whatever app was in front
            drag(from: CGPoint(x: num("x"), y: num("y")), to: CGPoint(x: num("x2"), y: num("y2")))
            return ["ok": true]
        case "scroll":
            guard AXIsProcessTrusted() else { return noAccessibility }
            focusChrome()                      // clicks and scrolls must land in Chrome, not in whatever app was in front
            scroll(at: CGPoint(x: num("x"), y: num("y")), dx: Int32(num("dx")), dy: Int32(num("dy")))
            return ["ok": true]
        case "key":
            guard AXIsProcessTrusted() else { return noAccessibility }
            return key((req["key"] as? String) ?? "", mods: (req["mods"] as? [String]) ?? [])
        case "type":
            guard AXIsProcessTrusted() else { return noAccessibility }
            focusChrome()
            Paster.paste((req["text"] as? String) ?? "")
            return ["ok": true]
        case "focus":
            focusChrome()
            return ["ok": true]
        default: return ["ok": false, "error": "unknown screen operation '\(op)'"]
        }
    }

    static let noAccessibility: [String: Any] = ["ok": false, "error":
        "Voice Control needs Accessibility permission to move the mouse and press keys (System Settings > Privacy & Security > Accessibility)."]

    // MARK: eyes
    /// Chrome's frontmost normal window, if any: (window id, bounds in global points).
    static func chromeWindow() -> (CGWindowID, CGRect)? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
        for w in list where (w[kCGWindowOwnerName as String] as? String) == "Google Chrome" && (w[kCGWindowLayer as String] as? Int) == 0 {
            guard let b = w[kCGWindowBounds as String] as? [String: CGFloat], let id = w[kCGWindowNumber as String] as? Int else { continue }
            let rect = CGRect(x: b["X"] ?? 0, y: b["Y"] ?? 0, width: b["Width"] ?? 0, height: b["Height"] ?? 0)
            if rect.width > 200 && rect.height > 200 { return (CGWindowID(id), rect) }
        }
        return nil
    }

    static func screenshot(target: String, mark: CGPoint? = nil) -> [String: Any] {
        guard CGPreflightScreenCaptureAccess() else {
            _ = CGRequestScreenCaptureAccess()
            return ["ok": false, "error": "Voice Control needs Screen Recording permission to see the screen (System Settings > Privacy & Security > Screen Recording), then ask again."]
        }
        var args = ["-x", "-t", "png"]
        var rect: CGRect
        if target == "chrome", let (id, bounds) = chromeWindow() {
            args += ["-o", "-l", String(id)]
            rect = bounds
        } else {
            rect = CGDisplayBounds(CGMainDisplayID())
        }
        let tmp = NSTemporaryDirectory() + "vc-shot-\(UUID().uuidString).png"
        args.append(tmp)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = args
        do { try p.run(); p.waitUntilExit() } catch { return ["ok": false, "error": "screencapture failed: \(error)"] }
        defer { try? FileManager.default.removeItem(atPath: tmp) }
        guard let image = NSImage(contentsOfFile: tmp), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            return ["ok": false, "error": "Couldn't capture the screen."]
        }
        // Downscale so the model sees a manageable image; the daemon maps its pixel coordinates back to screen points.
        let maxW: CGFloat = 1100
        let scale = min(1, maxW / CGFloat(cg.width))
        let w = Int(CGFloat(cg.width) * scale), h = Int(CGFloat(cg.height) * scale)
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
            return ["ok": false, "error": "Couldn't scale the screenshot."]
        }
        ctx.interpolationQuality = .high
        ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
        if let m = mark, rect.width > 0, rect.height > 0 {          // a red ring where the pointer last acted
            let px = (m.x - rect.origin.x) / rect.width * CGFloat(w), py = (1 - (m.y - rect.origin.y) / rect.height) * CGFloat(h)
            ctx.setStrokeColor(CGColor(red: 1, green: 0.1, blue: 0.1, alpha: 0.95)); ctx.setLineWidth(3)
            ctx.strokeEllipse(in: CGRect(x: px - 14, y: py - 14, width: 28, height: 28))
            ctx.setFillColor(CGColor(red: 1, green: 0.1, blue: 0.1, alpha: 0.95)); ctx.fillEllipse(in: CGRect(x: px - 2.5, y: py - 2.5, width: 5, height: 5))
        }
        guard let small = ctx.makeImage() else { return ["ok": false, "error": "Couldn't scale the screenshot."] }
        let rep = NSBitmapImageRep(cgImage: small)
        guard let jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.6]) else {
            return ["ok": false, "error": "Couldn't encode the screenshot."]
        }
        return ["ok": true, "image": jpeg.base64EncodedString(), "mime": "image/jpeg",
                "originX": rect.origin.x, "originY": rect.origin.y, "widthPt": rect.width, "heightPt": rect.height,
                "imgW": w, "imgH": h]
    }

    // MARK: hands
    static func post(_ type: CGEventType, _ p: CGPoint, button: CGMouseButton = .left, state: Int64 = 1) {
        guard let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: button) else { return }
        e.setIntegerValueField(.mouseEventClickState, value: state)
        e.post(tap: .cghidEventTap)
    }

    static func click(_ p: CGPoint, button: String, count: Int) {
        let right = button == "right"
        let (down, up, b): (CGEventType, CGEventType, CGMouseButton) = right ? (.rightMouseDown, .rightMouseUp, .right) : (.leftMouseDown, .leftMouseUp, .left)
        post(.mouseMoved, p); usleep(60_000)
        for n in 1...max(1, count) {
            post(down, p, button: b, state: Int64(n)); usleep(40_000)
            post(up, p, button: b, state: Int64(n)); usleep(60_000)
        }
    }

    static func drag(from a: CGPoint, to b: CGPoint) {
        post(.mouseMoved, a); usleep(60_000)
        post(.leftMouseDown, a); usleep(60_000)
        let steps = 12
        for i in 1...steps {
            let t = CGFloat(i) / CGFloat(steps)
            post(.leftMouseDragged, CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t)); usleep(20_000)
        }
        post(.leftMouseUp, b)
    }

    static func scroll(at p: CGPoint, dx: Int32, dy: Int32) {
        post(.mouseMoved, p); usleep(60_000)
        // Wheel units are pixels; positive dy scrolls the content up (i.e. "scroll down" the page uses a negative dy).
        CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)?.post(tap: .cghidEventTap)
    }

    static let keyCodes: [String: CGKeyCode] = [
        "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
        "left": 123, "right": 124, "down": 125, "up": 126, "pageup": 116, "pagedown": 121, "home": 115, "end": 119,
        "a": 0, "c": 8, "v": 9, "x": 7, "z": 6, "f": 3, "l": 37, "t": 17, "w": 13, "r": 15, "k": 40, "s": 1, "n": 45, "o": 31, "p": 35, "q": 12,
        "b": 11, "d": 2, "e": 14, "g": 5, "h": 4, "i": 34, "j": 38, "m": 46, "u": 32, "y": 16,
        "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
        "[": 33, "]": 30, "-": 27, "=": 24, ",": 43, ".": 47, "/": 44, "`": 50, "forwarddelete": 117,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f11": 103, "f12": 111,
    ]

    static func key(_ name: String, mods: [String]) -> [String: Any] {
        guard let code = keyCodes[name.lowercased()] else { return ["ok": false, "error": "unknown key '\(name)'"] }
        var flags: CGEventFlags = []
        for m in mods {
            switch m.lowercased() {
            case "cmd", "command": flags.insert(.maskCommand)
            case "shift": flags.insert(.maskShift)
            case "alt", "option": flags.insert(.maskAlternate)
            case "ctrl", "control": flags.insert(.maskControl)
            default: break
            }
        }
        focusChrome()
        let src = CGEventSource(stateID: .hidSystemState)
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
            e?.flags = flags
            e?.post(tap: .cghidEventTap)
            usleep(30_000)
        }
        return ["ok": true]
    }

    /// Keystrokes go to the frontmost app; make that Chrome (a no-op if it already is).
    static func focusChrome() {
        guard let chrome = NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome").first,
              !chrome.isActive else { return }
        chrome.activate(options: [])
        usleep(250_000)
    }
}
