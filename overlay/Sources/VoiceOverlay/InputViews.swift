import SwiftUI

/// The widget shows one of two separate input views, chosen by `model.usesMic`. They share only the pill's
/// styling and the three controls every input kind needs; nothing about the microphone exists in the typing view.

/// The capsule behind the status row.
struct PillShell<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        HStack(spacing: 8) { content }
            .padding(.horizontal, 14).padding(.vertical, 6)
            .background(Capsule().fill(.white.opacity(0.07)).overlay(Capsule().stroke(.white.opacity(0.10), lineWidth: 0.5)))
    }
}

/// What both input kinds show: the mode chip, the status label, and the type / new chat / fresh controls.
struct StatusRow: View {
    @ObservedObject var model: OverlayModel
    var body: some View {
        if model.mode != nil, model.isActive {
            Text(model.tag)
                .font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
                .padding(.horizontal, 7).padding(.vertical, 3)
                .background(Capsule().fill(model.palette.a.opacity(0.25)))
                .foregroundStyle(model.palette.a)
        }
        if !(model.phase == .idle && model.connected) {      // the resting pill is just the name
            StatusGlyph(phase: model.phase, color: model.connected ? model.palette.a : .orange, connected: model.connected, speaking: model.speaking, job: model.jobActive)
        }
        Text(model.label)
            .lineLimit(1).fixedSize()
            .font(.system(size: 10, weight: .semibold, design: .rounded)).tracking(2.4)
            .foregroundStyle(model.connected ? Color.white.opacity(model.phase == .idle ? 0.45 : 0.9) : Color.orange.opacity(0.9))
    }
}

struct CommonControls: View {
    @ObservedObject var model: OverlayModel
    var body: some View {
        TypeButton(on: model.typing) { model.typing.toggle() }
        NewChatButton { model.newConversation() }
        FreshButton { model.startFresh() }
    }
}

/// Talking: the waveform, the listening timer, the voice toggle and the spoken hints.
struct VoiceInputView: View {
    @ObservedObject var model: OverlayModel
    var body: some View {
        if model.isActive {
            Ribbons(palette: model.palette, level: model.level, phase: model.phase)
                .frame(height: 64)
                .transition(.opacity.combined(with: .scale(scale: 0.96)))
        }
        PillShell {
            StatusRow(model: model)
            if model.phase == .listening, let since = model.listenStart { ElapsedLabel(since: since) }
            VoiceToggle(on: model.voiceOn) { model.toggleVoice() }
            CommonControls(model: model)
        }
    }
}

/// What to press, under the caption. Talking is push-to-talk: F10 starts a recording and F10 again sends it, and
/// starting a recording is also how you interrupt. These hints belong to the voice kind only.
struct VoiceHint: View {
    @ObservedObject var model: OverlayModel
    var body: some View {
        if model.conversational && model.notice.isEmpty
            && (model.phase == .listening || model.phase == .thinking || model.phase == .acting || model.speaking || model.jobActive || model.phase == .music) {
            Text(model.phase == .listening ? "Press F10 again to send"
                 : model.phase == .music ? "Press F10 to talk over it"
                 : model.jobActive ? "Working on it. Press F10 to talk, or say “stop” to cancel"
                                   : "Press F10 to interrupt, or type")
                .font(.system(size: 10, design: .rounded)).foregroundStyle(.white.opacity(0.4))
        }
    }
}

/// Typing: no waveform, no timer, no voice controls, no hints; the type box is the input.
struct TypingInputView: View {
    @ObservedObject var model: OverlayModel
    var body: some View {
        PillShell {
            StatusRow(model: model)
            CommonControls(model: model)
        }
        TypeBox(model: model).transition(.opacity)
    }
}
