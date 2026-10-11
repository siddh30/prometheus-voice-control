import SwiftUI

struct SizeKey: PreferenceKey {
    static var defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { value = nextValue() }
}

struct OverlayView: View {
    @ObservedObject var model: OverlayModel
    var onSize: (CGSize) -> Void = { _ in }

    var body: some View {
        VStack(spacing: 10) {
            if model.route == "agent" || model.mode == "agent", let s = model.session, model.isActive {
                Text(s).font(.system(size: 9, weight: .medium, design: .rounded)).tracking(1.2)
                    .foregroundStyle(.white.opacity(0.5))
            }

            if model.isActive && model.usesMic {                          // a typed request has no waveform
                Ribbons(palette: model.palette, level: model.level, phase: model.phase)
                    .frame(height: 64)
                    .transition(.opacity.combined(with: .scale(scale: 0.96)))
            }

            HStack(spacing: 8) {
                if let mode = model.mode, model.isActive {
                    Text(model.tag)
                        .font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(Capsule().fill(model.palette.a.opacity(0.25)))
                        .foregroundStyle(model.palette.a)
                }
                if !(model.phase == .idle && model.connected) {      // the resting pill is just the name
                    StatusGlyph(phase: model.phase, color: model.connected ? model.palette.a : .orange, connected: model.connected, speaking: model.speaking, wake: model.wakeListening, job: model.jobActive)
                }
                Text(model.label)
                    .lineLimit(1).fixedSize()
                    .font(.system(size: 10, weight: .semibold, design: .rounded)).tracking(2.4)
                    .foregroundStyle(model.connected ? Color.white.opacity(model.phase == .idle ? 0.45 : 0.9) : Color.orange.opacity(0.9))
                if model.phase == .listening, let since = model.listenStart { ElapsedLabel(since: since) }
                VoiceToggle(on: model.voiceOn) { model.toggleVoice() }
                TypeButton(on: model.typing) { model.typing.toggle() }
                NewChatButton { model.newConversation() }
                FreshButton { model.startFresh() }
            }
            .padding(.horizontal, 14).padding(.vertical, 6)
            .background(Capsule().fill(.white.opacity(0.07)).overlay(Capsule().stroke(.white.opacity(0.10), lineWidth: 0.5)))

            if model.typing { TypeBox(model: model).transition(.opacity) }

            if !model.transcript.isEmpty {
                CaptionView(text: "“\(model.transcript)”").transition(.opacity)
            }

            if model.conversational && model.notice.isEmpty
                && (model.phase == .listening || model.phase == .thinking || model.phase == .acting || model.speaking || model.jobActive || model.phase == .music) {
                Text(model.phase == .music ? "Say “Prometheus …” (or press F10) to talk to me"
                     : model.jobActive ? "Working on it. Keep talking, say “stop” to cancel"
                     : model.phase == .listening && !model.speaking ? "Say “goodbye” or press F10 to end"
                                                                    : (model.duplex == "half" ? "Press F10 to interrupt" : "Just start talking (or press F10) to interrupt"))
                    .font(.system(size: 10, design: .rounded)).foregroundStyle(.white.opacity(0.4))
            }

            if model.phase == .music && !model.musicTitle.isEmpty {
                Text("♪ \(model.musicTitle)").font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(0.85))
                    .lineLimit(2).multilineTextAlignment(.center).frame(maxWidth: 380)
            }

            if model.needsPermission {
                Text("Allow Voice Control in System Settings → Privacy & Security → Accessibility and Input Monitoring")
                    .font(.system(size: 11, design: .rounded)).foregroundStyle(.orange.opacity(0.95))
                    .multilineTextAlignment(.center).lineLimit(3).frame(maxWidth: 380)
            }

            if !model.notice.isEmpty {
                Text(model.notice)
                    .font(.system(size: 11, design: .rounded)).foregroundStyle(.orange.opacity(0.9))
                    .multilineTextAlignment(.center).lineLimit(3).frame(maxWidth: 380)
            }

            if model.expanded { AgentPanel(model: model).transition(.opacity.combined(with: .move(edge: .bottom))) }
        }
        .padding(.horizontal, 22).padding(.vertical, model.isActive ? 16 : 10)
        .frame(width: model.isActive ? 440 : nil)
        .fixedSize(horizontal: !model.isActive, vertical: false)       // idle pill hugs its text, whatever the label
        .background(
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).fill(Color(red: 0.08, green: 0.09, blue: 0.10).opacity(0.93)))   // near-solid: reads the same on light and dark backgrounds
                .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).stroke(model.isActive ? model.palette.a.opacity(0.55) : .white.opacity(0.10), lineWidth: model.isActive ? 1.2 : 0.6))
        )
        .animation(.spring(response: 0.4, dampingFraction: 0.82), value: model.isActive)
        .animation(.spring(response: 0.4, dampingFraction: 0.82), value: model.expanded)
        .animation(.easeInOut(duration: 0.25), value: model.transcript)
        .padding(20)
        .frame(width: 480, height: 640, alignment: .bottom)
    }
}

struct AgentPanel: View {
    @ObservedObject var model: OverlayModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !model.checklist.isEmpty {
                HStack {
                    Label("CHECKLIST", systemImage: "checklist").font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
                    Spacer()
                    Text("\(model.checklist.filter { $0.status == "done" || $0.status == "skipped" }.count) of \(model.checklist.count)")
                        .font(.system(size: 9, weight: .semibold, design: .rounded)).tracking(0.6)
                }
                .foregroundStyle(.white.opacity(0.5))
                ForEach(model.checklist) { item in
                    HStack(alignment: .top, spacing: 8) {
                        switch item.status {
                        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
                        case "in_progress": Spinner(color: model.palette.a)
                        case "skipped": Image(systemName: "minus.circle").foregroundStyle(.white.opacity(0.35))
                        default: Image(systemName: "circle").foregroundStyle(.white.opacity(0.35))
                        }
                        Text(item.text).font(.system(size: 12, weight: item.status == "in_progress" ? .semibold : .regular, design: .rounded))
                            .foregroundStyle(.white.opacity(item.status == "done" || item.status == "skipped" ? 0.5 : 0.95))
                            .strikethrough(item.status == "skipped")
                    }
                    .font(.system(size: 12))
                }
                if let now = model.steps.last(where: { !$0.done }) {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.right").font(.system(size: 8, weight: .bold))
                        Text(now.text).lineLimit(1).truncationMode(.middle)
                    }
                    .font(.system(size: 10, design: .rounded)).foregroundStyle(model.palette.a.opacity(0.85))
                }
            }
            if model.checklist.isEmpty && !model.plan.isEmpty && model.steps.isEmpty {
                Label("PLAN", systemImage: "list.bullet").font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
                    .foregroundStyle(.white.opacity(0.5))
                ForEach(Array(model.plan.enumerated()), id: \.offset) { i, s in
                    Text("\(i + 1). \(s)").font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(0.85))
                }
            }
            ForEach(model.checklist.isEmpty ? model.steps : []) { step in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: step.failed ? "xmark.circle.fill" : (step.done ? "checkmark.circle.fill" : "circle.dotted"))
                        .foregroundStyle(step.failed ? Color.red : (step.done ? Color.green : model.palette.a)).font(.system(size: 11))
                    Text(step.text).font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(step.done ? 0.6 : 0.95))
                        .lineLimit(2)
                }
            }
            if let ask = model.approval {
                VStack(alignment: .leading, spacing: 8) {
                    Text(ask).font(.system(size: 12, weight: .medium, design: .rounded)).foregroundStyle(.white)
                    HStack(spacing: 8) {
                        PillButton(title: "Deny", fill: .white.opacity(0.10), fg: .white.opacity(0.85)) { model.respondApproval(false) }
                        PillButton(title: "Approve", fill: model.palette.a, fg: .white) { model.respondApproval(true) }
                    }
                }
                .padding(10).background(RoundedRectangle(cornerRadius: 12).fill(.white.opacity(0.07)))
            }
            if !model.visibleResult.isEmpty {
                Divider().overlay(.white.opacity(0.1))
                let body = Text(model.visibleResult).font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(0.92))
                    .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                if model.visibleResult.count > 420 {                 // long answers scroll; short ones size to their text
                    ScrollView { body }.frame(height: 150)
                } else {
                    body.fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16).fill(.black.opacity(0.25)))
    }
}

struct PillButton: View {
    let title: String
    let fill: Color
    let fg: Color
    let action: () -> Void

    var body: some View {
        Text(title)
            .font(.system(size: 12, weight: .semibold, design: .rounded))
            .foregroundStyle(fg)
            .padding(.horizontal, 16).padding(.vertical, 6)
            .background(Capsule().fill(fill))
            .contentShape(Capsule())
            .onTapGesture(perform: action)
    }
}


/// Speak replies aloud, or text only (handy on speakers: no sound out means nothing for the mic to hear).
struct VoiceToggle: View {
    let on: Bool
    let action: () -> Void
    var body: some View {
        Image(systemName: on ? "speaker.wave.2.fill" : "speaker.slash.fill")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(on ? Color.white.opacity(0.75) : Color.orange.opacity(0.95))
            .frame(width: 22, height: 22)
            .background(Circle().fill(on ? Color.white.opacity(0.08) : Color.orange.opacity(0.18)))
            .contentShape(Circle())
            .onTapGesture(perform: action)
            .help(on ? "Voice replies on. Click for text only." : "Text only. Click to hear replies.")
    }
}

/// What you said / what Prometheus is saying. Grows with the text; very long text scrolls and follows the newest words.
struct CaptionView: View {
    let text: String
    private var label: some View {
        Text(text)
            .font(.system(size: 13, weight: .regular, design: .rounded))
            .foregroundStyle(.white.opacity(0.92))
            .multilineTextAlignment(.center)
            .frame(width: 380)
            .fixedSize(horizontal: false, vertical: true)
    }
    var body: some View {
        if text.count < 330 {
            label
        } else {
            ScrollViewReader { proxy in
                ScrollView(.vertical, showsIndicators: false) { label.id("end") }
                    .frame(width: 380, height: 230)
                    .onChange(of: text) { _ in proxy.scrollTo("end", anchor: .bottom) }
            }
        }
    }
}


/// Clears everything Prometheus remembers. Two clicks, so it can't happen by accident: the first arms it ("CLEAR?"), the second does it.
struct FreshButton: View {
    let action: () -> Void
    @State private var armed = false
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: armed ? "trash.fill" : "arrow.counterclockwise")
                .font(.system(size: 10, weight: .semibold))
            if armed { Text("CLEAR ALL MEMORY?").font(.system(size: 8, weight: .bold, design: .rounded)).tracking(0.8) }
        }
        .foregroundStyle(armed ? Color.red.opacity(0.95) : Color.white.opacity(0.6))
        .padding(.horizontal, armed ? 8 : 0)
        .frame(minWidth: 22, minHeight: 22)
        .background(Capsule().fill(armed ? Color.red.opacity(0.18) : Color.white.opacity(0.08)))
        .contentShape(Capsule())
        .onTapGesture {
            if armed { armed = false; action() }
            else {
                armed = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { armed = false }
            }
        }
        .help(armed ? "Click again to forget everything and start fresh" : "Start fresh: forget everything Prometheus remembers")
    }
}


/// Opens the box for typing to Prometheus (to interrupt it, or when talking isn't convenient).
struct TypeButton: View {
    let on: Bool
    let action: () -> Void
    var body: some View {
        Image(systemName: "keyboard")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(on ? Color.white : Color.white.opacity(0.6))
            .frame(width: 22, height: 22)
            .background(Circle().fill(on ? Color.white.opacity(0.22) : Color.white.opacity(0.08)))
            .contentShape(Circle())
            .onTapGesture(perform: action)
            .help("Type to Prometheus (interrupts it)")
    }
}

struct TypeBox: View {
    @ObservedObject var model: OverlayModel
    @State private var text = ""
    @FocusState private var focused: Bool
    var body: some View {
        HStack(spacing: 8) {
            TextField("Type to Prometheus, press Return…", text: $text)
                .textFieldStyle(.plain)
                .font(.system(size: 13, design: .rounded))
                .foregroundStyle(.white)
                .focused($focused)
                .onSubmit { model.sendTyped(text); text = "" }
                .onExitCommand { model.typing = false }
            Image(systemName: "return").font(.system(size: 10, weight: .semibold)).foregroundStyle(.white.opacity(0.45))
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .frame(width: 380)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.white.opacity(0.10)))
        .onAppear { DispatchQueue.main.async { focused = true } }
    }
}


/// One click: a new conversation with a clean context (use when Prometheus gets stuck). It keeps what it knows about you.
struct NewChatButton: View {
    let action: () -> Void
    var body: some View {
        Image(systemName: "plus.bubble")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(Color.white.opacity(0.6))
            .frame(width: 22, height: 22)
            .background(Circle().fill(Color.white.opacity(0.08)))
            .contentShape(Circle())
            .onTapGesture(perform: action)
            .help("New conversation: clean context, keeps what I know about you")
    }
}
