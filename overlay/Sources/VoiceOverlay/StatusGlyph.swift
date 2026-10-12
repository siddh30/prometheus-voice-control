import SwiftUI

/// The at-a-glance signal: one distinct, animated icon per state, readable without reading the label.
struct StatusGlyph: View {
    let phase: Phase
    let color: Color
    let connected: Bool
    var speaking = false
    var job = false

    var body: some View {
        Group {
            if !connected {
                Spinner(color: .orange)
            } else if speaking && phase != .listening && phase != .transcribing {
                PulsingDot(color: color)                      // the speaker icon next to it is the voice on/off button
            } else {
                switch phase {
                case .idle:
                    Circle().fill(.white.opacity(0.35)).frame(width: 7, height: 7)
                case .listening:
                    if job { Spinner(color: color) } else { PulsingDot(color: color) }
                case .transcribing, .handling, .thinking, .acting:
                    Spinner(color: color)
                case .typing:
                    Image(systemName: "keyboard").font(.system(size: 11, weight: .semibold)).foregroundStyle(color)
                case .awaiting_approval:
                    Image(systemName: "hand.raised.fill").font(.system(size: 11)).foregroundStyle(color)
                case .done:
                    Image(systemName: "checkmark.circle.fill").font(.system(size: 13)).foregroundStyle(color)
                case .music:
                    Image(systemName: "music.note").font(.system(size: 12, weight: .semibold)).foregroundStyle(color)
                case .error:
                    Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 11)).foregroundStyle(color)
                }
            }
        }
        .frame(width: 16, height: 16)
    }
}

struct PulsingDot: View {
    let color: Color
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            let pulse = 0.5 + 0.5 * sin(t * 5)
            ZStack {
                Circle().fill(color.opacity(0.30 * (1 - pulse))).frame(width: 16, height: 16).scaleEffect(1 + pulse * 0.4)
                Circle().fill(color).frame(width: 9, height: 9).shadow(color: color, radius: 4)
            }
        }
    }
}

struct Spinner: View {
    let color: Color
    var body: some View {
        TimelineView(.animation) { tl in
            let angle = (tl.date.timeIntervalSinceReferenceDate * 360).truncatingRemainder(dividingBy: 360)
            Circle().trim(from: 0.0, to: 0.72)
                .stroke(color, style: StrokeStyle(lineWidth: 2.2, lineCap: .round))
                .frame(width: 12, height: 12).rotationEffect(.degrees(angle))
        }
    }
}

/// "0:07" while recording, so a toggle-mode recording is never a mystery.
struct ElapsedLabel: View {
    let since: Date
    var body: some View {
        TimelineView(.periodic(from: since, by: 0.5)) { tl in
            let s = max(0, Int(tl.date.timeIntervalSince(since)))
            Text(String(format: "%d:%02d", s / 60, s % 60))
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.75))
        }
    }
}
