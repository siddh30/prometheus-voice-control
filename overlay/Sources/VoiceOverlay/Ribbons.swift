import SwiftUI

/// Siri-style layered ribbons: three mirrored sine waves with additive blending over a bright core line.
struct Ribbons: View {
    let palette: Palette
    let level: Double
    let phase: Phase

    var body: some View {
        TimelineView(.animation(minimumInterval: phase == .listening ? 1.0 / 30.0 : 1.0 / 12.0)) { tl in   // full speed only while it follows your voice
            Canvas { ctx, size in
                let t = tl.date.timeIntervalSinceReferenceDate
                let w = size.width, h = size.height, mid = h / 2
                // Idle breathes gently; transcribing/thinking pulse on their own; listening follows the mic.
                let synthetic: Double
                switch phase {
                case .idle: synthetic = 0.08 + 0.03 * sin(t * 1.4)
                case .transcribing, .handling, .thinking, .acting: synthetic = 0.30 + 0.12 * sin(t * 3.0)
                case .done: synthetic = 0.20 + 0.05 * sin(t * 1.6)
                case .music: synthetic = 0.22 + 0.10 * sin(t * 2.4) * sin(t * 0.7)     // a gentle sway, like the song
                case .awaiting_approval: synthetic = 0.18 + 0.06 * sin(t * 2.0)
                default: synthetic = 0.15
                }
                let amp = phase == .listening ? max(0.10, level) : synthetic

                let layers: [(Color, Double, Double, Double)] = [
                    (palette.a, 1.00, 1.7, 0.0),
                    (palette.b, 0.78, 2.3, 1.7),
                    (palette.c, 0.60, 3.1, 3.4),
                ]
                ctx.blendMode = .plusLighter
                // A soft halo drawn as a wider, fainter copy of each ribbon: looks like a glow but needs no GPU blur filter,
                // which at 30 frames a second was a large part of the desktop's drawing load.
                for (grow, strength) in [(1.30, 0.20), (1.0, 1.0)] {
                for (color, scale0, speed, offset) in layers {
                    let scale = scale0 * grow
                    for mirror in [1.0, -1.0] {
                        var path = Path()
                        path.move(to: CGPoint(x: 0, y: mid))
                        let steps = 90
                        for i in 0...steps {
                            let x = w * Double(i) / Double(steps)
                            let u = Double(i) / Double(steps)
                            let envelope = pow(sin(.pi * u), 2.2)       // taper to the edges
                            let wave = sin(u * 9 + t * speed + offset) * 0.6 + sin(u * 5 - t * speed * 0.7 + offset) * 0.4
                            let y = mid + mirror * (mid * 0.92) * amp * scale * envelope * (0.55 + 0.45 * wave)
                            path.addLine(to: CGPoint(x: x, y: y))
                        }
                        path.addLine(to: CGPoint(x: w, y: mid))
                        path.closeSubpath()
                        ctx.fill(path, with: .linearGradient(
                            Gradient(colors: [color.opacity(0.0), color.opacity(0.85 * strength), color.opacity(0.0)]),
                            startPoint: CGPoint(x: 0, y: mid), endPoint: CGPoint(x: w, y: mid)))
                    }
                }
                }
                // bright core
                var core = Path()
                core.addRoundedRect(in: CGRect(x: w * 0.04, y: mid - 1.2, width: w * 0.92, height: 2.4), cornerSize: CGSize(width: 1.2, height: 1.2))
                ctx.fill(core, with: .linearGradient(
                    Gradient(colors: [.white.opacity(0), .white.opacity(0.95), .white.opacity(0)]),
                    startPoint: CGPoint(x: 0, y: mid), endPoint: CGPoint(x: w, y: mid)))
            }
        }
    }
}
