import SwiftUI

/// What the widget shows. The Switcher updates it; the widget only draws it and calls back.
@MainActor
final class SwitchModel: ObservableObject {
    @Published var running: Set<Mode> = []
    @Published var expanded = false             // true while the card is on screen (and only then does F12 work)
    @Published var message = ""                 // "Closing Voice Control…", or a problem in plain words
    @Published var failed = false
    @Published var busy = false
    @Published var loginOn = false
    @Published var keyActive = false            // F12 works anywhere (the one-time keyboard permissions are granted)

    var onCollapse: () -> Void = {}
    var onPrivacy: () -> Void = {}
    var onPick: (Mode) -> Void = { _ in }
    var onLogin: () -> Void = {}
    var onQuit: () -> Void = {}
}

enum WidgetSize {
    static let card = CGSize(width: 340, height: 322)
}

/// The floating card that opens under the menu-bar icon. Same look as the Voice Control widget: a near-solid dark surface that reads on
/// light and dark backgrounds. When it is not needed it is not on screen at all.
struct SwitchWidget: View {
    @ObservedObject var model: SwitchModel
    var body: some View { SwitchCard(model: model, framed: true) }
}

/// The card with both apps. Used inside the floating widget (framed with its own dark surface) and inside the menu-bar popover.
struct SwitchCard: View {
    @ObservedObject var model: SwitchModel
    var framed: Bool

    private var surface: Color { Color(red: 0.08, green: 0.09, blue: 0.10).opacity(0.96) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "arrow.left.arrow.right.circle.fill").font(.system(size: 24)).foregroundStyle(SwarmLikeAccent.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Prometheus Switch").font(.system(size: 14, weight: .semibold, design: .rounded)).foregroundStyle(.white)
                    Text(model.keyActive ? "Press F12 anywhere" : "Press F12 while this is open").font(.system(size: 10, design: .rounded)).foregroundStyle(.white.opacity(0.5))
                }
                Spacer()
                Image(systemName: "chevron.down.circle.fill").font(.system(size: 18)).foregroundStyle(.white.opacity(0.35))
                    .onTapGesture { model.onCollapse() }.help("Close")
            }

            ForEach(Mode.allCases, id: \.self) { mode in
                ModeRow(mode: mode, active: model.running.contains(mode), busy: model.busy) { model.onPick(mode) }
            }

            HStack(spacing: 8) {                                                       // always one line tall, so the card never jumps
                if model.busy { ProgressView().controlSize(.small).colorScheme(.dark) }
                else if !model.message.isEmpty {
                    Image(systemName: model.failed ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                        .foregroundStyle(model.failed ? Color.orange : Color.green)
                }
                Text(model.message).font(.system(size: 11, design: .rounded)).foregroundStyle(.white.opacity(0.85)).lineLimit(3)
                Spacer(minLength: 0)
            }
            .frame(height: 40, alignment: .topLeading)

            Divider().overlay(.white.opacity(0.1))
            if !model.keyActive {
                Button { model.onPrivacy() } label: { Label("Allow F12 everywhere…", systemImage: "keyboard") }
                    .buttonStyle(.link).font(.system(size: 11, design: .rounded))
            }
            HStack {
                Toggle("Open at login", isOn: Binding(get: { model.loginOn }, set: { _ in model.onLogin() }))
                    .toggleStyle(.checkbox).font(.system(size: 11, design: .rounded)).foregroundStyle(.white.opacity(0.8))
                Spacer()
                Button("Quit") { model.onQuit() }.buttonStyle(.plain).font(.system(size: 11, design: .rounded)).foregroundStyle(.white.opacity(0.5))
            }
        }
        .padding(16)
        .frame(width: WidgetSize.card.width, height: WidgetSize.card.height, alignment: .top)
        .background(framed ? AnyView(RoundedRectangle(cornerRadius: 24, style: .continuous).fill(surface)
            .overlay(RoundedRectangle(cornerRadius: 24, style: .continuous).stroke(.white.opacity(0.12), lineWidth: 0.8))) : AnyView(surface))
    }
}



private enum SwarmLikeAccent { static let accent = Color(red: 0.55, green: 0.40, blue: 1.0) }

private struct ModeRow: View {
    let mode: Mode
    let active: Bool
    let busy: Bool
    let pick: () -> Void

    private var symbol: String { mode == .single ? "waveform" : "point.3.connected.trianglepath.dotted" }
    private var blurb: String { mode == .single ? "One computer, push-to-talk" : "Your computers working together" }

    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                Circle().fill(active ? Color.green.opacity(0.18) : Color.white.opacity(0.07)).frame(width: 38, height: 38)
                Image(systemName: symbol).font(.system(size: 16, weight: .semibold)).foregroundStyle(active ? Color.green : Color.white.opacity(0.5))
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(mode.appName).font(.system(size: 13, weight: .semibold, design: .rounded)).foregroundStyle(.white).lineLimit(1).minimumScaleFactor(0.85)
                HStack(spacing: 5) {
                    Circle().fill(active ? Color.green : Color.white.opacity(0.3)).frame(width: 6, height: 6)
                    Text(active ? "Running" : "Not running").font(.system(size: 10, design: .rounded)).foregroundStyle(.white.opacity(0.55))
                }
                Text(blurb).font(.system(size: 9, design: .rounded)).foregroundStyle(.white.opacity(0.35))
            }
            Spacer(minLength: 4)
            if active {
                Text("IN USE").font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.2).foregroundStyle(Color.green)
            } else {
                Button("Switch", action: pick).buttonStyle(.borderedProminent).controlSize(.small).disabled(busy)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14).fill(active ? Color.green.opacity(0.08) : Color.white.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(active ? Color.green.opacity(0.35) : Color.clear, lineWidth: 1))
    }
}
