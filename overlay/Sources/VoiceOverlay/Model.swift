import SwiftUI

enum Phase: String {
    case idle, listening, transcribing, handling, typing, thinking, acting, awaiting_approval, error, done, music

    var label: String {
        switch self {
        case .idle: return "IDLE"
        case .listening: return "LISTENING"
        case .transcribing: return "TRANSCRIBING"
        case .handling, .thinking: return "THINKING"
        case .typing: return "TYPING"
        case .acting: return "WORKING"
        case .awaiting_approval: return "APPROVE?"
        case .error: return "ERROR"
        case .done: return "DONE"
        case .music: return "PLAYING"
        }
    }
}

struct Palette {
    let a: Color, b: Color, c: Color
    static let idle = Palette(a: .white.opacity(0.55), b: .gray.opacity(0.5), c: .white.opacity(0.35))
    static let listening = Palette(a: Color(red: 1.0, green: 0.30, blue: 0.40), b: Color(red: 1.0, green: 0.55, blue: 0.35), c: Color(red: 0.95, green: 0.25, blue: 0.65))
    static let transcribing = Palette(a: Color(red: 1.0, green: 0.78, blue: 0.30), b: Color(red: 1.0, green: 0.62, blue: 0.20), c: Color(red: 1.0, green: 0.90, blue: 0.55))
    static let agent = Palette(a: Color(red: 0.55, green: 0.40, blue: 1.0), b: Color(red: 0.35, green: 0.55, blue: 1.0), c: Color(red: 0.80, green: 0.45, blue: 1.0))
    static let action = Palette(a: Color(red: 0.20, green: 0.80, blue: 0.95), b: Color(red: 0.25, green: 0.95, blue: 0.70), c: Color(red: 0.35, green: 0.60, blue: 1.0))
    static let result = Palette(a: Color(red: 0.74, green: 0.45, blue: 1.0), b: Color(red: 0.95, green: 0.50, blue: 0.90), c: Color(red: 0.58, green: 0.52, blue: 1.0))
    static let convo = Palette(a: Color(red: 0.25, green: 0.85, blue: 0.80), b: Color(red: 0.35, green: 0.70, blue: 1.0), c: Color(red: 0.45, green: 0.95, blue: 0.65))
    static let done = Palette(a: Color(red: 0.30, green: 0.90, blue: 0.55), b: Color(red: 0.40, green: 0.95, blue: 0.75), c: Color(red: 0.55, green: 1.0, blue: 0.60))
    static let error = Palette(a: Color(red: 1.0, green: 0.25, blue: 0.25), b: Color(red: 1.0, green: 0.45, blue: 0.30), c: Color(red: 0.8, green: 0.15, blue: 0.25))
}

struct ChecklistItem: Identifiable {
    let id = UUID()
    var text: String
    var status: String          // pending | in_progress | done | skipped
}

struct Step: Identifiable {
    let id = UUID()
    var text: String
    var done: Bool
    var failed = false
}

@MainActor
final class OverlayModel: ObservableObject {
    @Published var phase: Phase = .idle
    @Published var mode: String? = nil
    @Published var level: Double = 0          // smoothed 0...1
    @Published var transcript: String = ""
    @Published var notice: String = ""
    @Published var plan: [String] = []
    @Published var steps: [Step] = []
    @Published var result: String = ""
    @Published var connected = false
    @Published var typedTurn = false          // the current request was typed (see usesMic)
    @Published var flash: Palette? = nil      // brief "done" / error tint
    @Published var approval: String? = nil
    @Published var session: String? = nil     // agent session chip
    @Published var listenStart: Date? = nil
    @Published var speaking = false
    @Published var checklist: [ChecklistItem] = []
    @Published var typing = false             // the "type to Prometheus" box is open
    @Published var voiceOn = true             // speak replies aloud, or show them as text only (e.g. on speakers)
    @Published var musicTitle = ""
    @Published var duplex = "full"            // full | echo | half: whether talking over the assistant works by voice
    @Published var jobActive = false          // the agent is working in the background while we keep talking
    @Published var jobText = ""
    @Published var route: String? = nil       // assistant decision: action | agent | chat
    @Published var wakeListening = false
    @Published var needsPermission = false     // hotkeys can't be captured yet

    private var clearTask: Task<Void, Never>?
    var send: ((String) -> Void)?

    /// The hands-free conversation modes (they keep listening, show replies, can be talked over).
    var conversational: Bool { mode == "assistant" || mode == "conversation" || mode == "prometheus" }

    /// What the pill says. After a run finishes it names the outcome instead of dropping straight to IDLE.
    var label: String {
        if !connected { return "CONNECTING" }
        if typedTurn && phase == .listening && !speaking { return jobActive ? "WORKING" : "READY" }    // typed: nothing is being listened to
        if speaking && phase != .listening && phase != .transcribing { return "SPEAKING" }
        if jobActive && (phase == .listening || phase == .transcribing) { return "WORKING" }   // never "thinking" while talking
        if phase == .idle { return isActive ? "IDLE" : "PROMETHEUS" }   // resting pill carries the name; once the card is open the tag already says it
        guard phase == .done else { return phase.label }
        switch mode {
        case "dictate": return "TYPED"
        case "agent": return "RESULT"
        case "assistant", "conversation", "prometheus": return "ENDED"
        default: return "DONE"
        }
    }

    var isActive: Bool { typing || phase == .music || needsPermission || speaking || jobActive || !checklist.isEmpty || phase != .idle || !transcript.isEmpty || !notice.isEmpty || !steps.isEmpty || !result.isEmpty }
    var expanded: Bool {
        let panelContent = !plan.isEmpty || !steps.isEmpty || !result.isEmpty || approval != nil
        return (mode == "agent" || conversational) && (panelContent || !checklist.isEmpty)
    }

    /// Typing and talking are two separate input kinds. Anything about the microphone (waveform, level, "listening")
    /// belongs only to the voice kind; a typed request, or the box being open, never shows it.
    var usesMic: Bool { !typing && !typedTurn }

    /// The result line, unless it only restates the checklist or steps already on screen ("Opened linkedin.com" twice).
    /// The checklist is the progress report, not an answer: a result shows only when it adds something to it.
    var visibleResult: String {
        let words = { (t: String) -> Set<String> in
            Set(t.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init))
        }
        let r = words(result)
        if r.isEmpty { return "" }
        let shown = checklist.map(\.text) + steps.map(\.text)
        if shown.isEmpty { return result }
        let known = shown.reduce(into: Set<String>()) { $0.formUnion(words($1)) }
        let fresh = r.subtracting(known).subtracting(["the", "a", "an", "i", "i've", "ive", "it", "is", "to", "and", "done", "ok", "okay", "https", "http", "www", "com"])
        return fresh.count * 5 <= r.count ? "" : result       // at most a fifth of its words are new: it is the same report again
    }

    /// Chip text: what the assistant decided to do with your request, else the mode.
    var tag: String { (route.map { $0 == "chat" ? "CHAT" : $0.uppercased() } ?? mode ?? "").uppercased() }

    var palette: Palette {
        if let f = flash { return f }
        switch phase {
        case .idle: return .idle
        case .listening where jobActive: return .agent
        case .listening:
            switch mode {
            case "agent": return .agent
            case "action": return .action
            case "assistant", "conversation", "prometheus": return .convo
            default: return .listening
            }
        case .transcribing: return .transcribing
        case .handling, .thinking, .acting, .awaiting_approval:
            if mode == "prometheus" { return phase == .acting || !checklist.isEmpty ? .agent : .convo }   // violet while doing, teal while talking
            switch route ?? mode {
            case "action": return .action
            case "chat", "assistant", "conversation": return .convo
            default: return .agent
            }
        case .typing: return .done
        case .done: return .result
        case .music: return .convo
        case .error: return .error
        }
    }

    func handle(type: String, data: [String: Any]) {
        switch type {
        case "level":
            let v = (data["value"] as? Double) ?? 0
            level = level * 0.55 + v * 0.45
            if v > 0.15 && typedTurn { typedTurn = false }      // you started talking: the waveform is useful again
        case "state":
            let s = Phase(rawValue: (data["state"] as? String) ?? "idle") ?? .idle
            let newMode = (data["mode"] as? String) ?? mode
            if s == (pendingPhase ?? phase) && newMode == mode { return }       // duplicate: changing nothing must show nothing
            mode = newMode
            requestPhase(s)
        case "transcript":
            if let m = data["mode"] as? String { mode = m }   // e.g. the wake word arrives before any state event
            if conversational {
                if !jobActive { plan = []; steps = [] }      // keep the agent's progress visible while you chat
                result = ""; approval = nil; route = nil
            }
            transcript = (data["text"] as? String) ?? ""
        case "notice":
            notice = (data["text"] as? String) ?? ""
            scheduleClear(after: 3)
        case "error":
            notice = (data["message"] as? String) ?? "Error"
            flashFor(.error)
            scheduleClear(after: 5)
        case "plan":
            plan = (data["steps"] as? [String]) ?? []
        case "step":
            let text = (data["text"] as? String) ?? ""
            let done = (data["done"] as? Bool) ?? false
            let failed = (data["failed"] as? Bool) ?? false
            if let i = steps.lastIndex(where: { $0.text == text }) { steps[i].done = done; steps[i].failed = failed }
            else { steps.append(Step(text: text, done: done, failed: failed)) }
        case "result":
            if speaking { break }                       // the caption is being revealed word by word as it is spoken; don't flash the whole text
            result = (data["text"] as? String) ?? ""
            flashFor(.done)
        case "approval":
            approval = (data["text"] as? String) ?? "Approve this action?"
            phase = .awaiting_approval
        case "speech_text":
            result = (data["text"] as? String) ?? result      // words appear as they are spoken
        case "checklist":
            let items = (data["items"] as? [[String: Any]]) ?? []
            checklist = items.map { ChecklistItem(text: ($0["text"] as? String) ?? "", status: ($0["status"] as? String) ?? "pending") }
        case "job":
            jobActive = (data["active"] as? Bool) ?? false
            jobText = (data["text"] as? String) ?? jobText
        case "voice":
            voiceOn = (data["on"] as? Bool) ?? true
        case "music":
            musicTitle = (data["on"] as? Bool) == true ? ((data["title"] as? String) ?? "") : ""
        case "duplex":
            duplex = (data["mode"] as? String) ?? "full"
        case "route":
            route = data["kind"] as? String
        case "wake":
            wakeListening = (data["listening"] as? Bool) ?? false
        case "speaking":
            speaking = (data["on"] as? Bool) ?? false
        case "session":
            session = data["label"] as? String
        default: break
        }
    }


    // Rapid state changes (listening -> transcribing -> thinking within a few ms) used to make the widget blink.
    // A change is shown at once if the last one was a moment ago; otherwise the newest request waits its turn.
    private var lastPhaseChange = Date.distantPast
    private var pendingPhase: Phase?
    private var pendingTask: Task<Void, Never>?
    private let minDwell: TimeInterval = 0.22

    private func requestPhase(_ s: Phase) {
        let wait = minDwell - Date().timeIntervalSince(lastPhaseChange)
        if wait <= 0 || s == .idle || s == .error {
            pendingTask?.cancel(); pendingPhase = nil
            applyPhase(s)
        } else {
            pendingPhase = s
            pendingTask?.cancel()
            pendingTask = Task {
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                guard !Task.isCancelled, let p = self.pendingPhase else { return }
                self.pendingPhase = nil
                self.applyPhase(p)
            }
        }
    }

    private func applyPhase(_ s: Phase) {
        lastPhaseChange = Date()
        let continuous = conversational
        if s == .listening {
            if !continuous || phase == .idle || phase == .done { resetRun() }
            if phase != .listening { listenStart = Date() }        // keep the timer running if we were already listening
        } else { listenStart = nil }
        let hadOutput = !transcript.isEmpty || !steps.isEmpty || !result.isEmpty
        if s == .idle && hadOutput && notice.isEmpty && phase != .idle && phase != .listening {
            phase = .done                       // hold a purple "result" state before fading to idle
            level = 0
            scheduleClear(after: mode == "agent" ? 15 : 5)
        } else {
            phase = s
            if s == .idle { level = 0; scheduleClear() }
        }
    }

    func toggleVoice() {
        voiceOn.toggle()                                   // optimistic; the daemon confirms with a "voice" event
        send?("{\"type\":\"voice\",\"on\":\(voiceOn)}")
    }

    /// Send a typed message: interrupts whatever Prometheus is saying or doing and becomes the newest instruction.
    func sendTyped(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        typing = false
        if !t.isEmpty { typedTurn = true }
        guard !t.isEmpty, let data = try? JSONSerialization.data(withJSONObject: ["type": "text", "text": t]),
              let json = String(data: data, encoding: .utf8) else { return }
        send?(json)
    }

    /// A new, clean conversation (nothing from the old thread carries over); what Prometheus knows about you stays.
    func newConversation() { send?("{\"type\":\"newchat\"}") }

    /// Forget everything Prometheus remembers (conversation, facts, sessions) and start with a clean slate.
    func startFresh() { send?("{\"type\":\"fresh\"}") }

    func respondApproval(_ ok: Bool) {
        approval = nil
        send?("{\"type\":\"approval\",\"approved\":\(ok)}")
    }

    private func resetRun() {
        clearTask?.cancel()
        transcript = ""; notice = ""; plan = []; steps = []; result = ""; approval = nil; route = nil; checklist = []
    }

    private func flashFor(_ p: Palette) {
        flash = p
        Task { try? await Task.sleep(nanoseconds: 900_000_000); self.flash = nil }
    }

    private func scheduleClear(after seconds: Double = 6) {
        clearTask?.cancel()
        clearTask = Task {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled, self.phase == .idle || self.phase == .done, self.approval == nil else { return }
            // Keep agent results visible longer; they are read, not glanced at.
            
            self.transcript = ""; self.notice = ""; self.plan = []; self.steps = []; self.result = ""; self.checklist = []
            self.typedTurn = false
            self.phase = .idle
        }
    }
}
