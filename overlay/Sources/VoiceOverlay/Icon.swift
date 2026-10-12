import SwiftUI
import AppKit

struct AppIconView: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.10, green: 0.11, blue: 0.20), Color(red: 0.03, green: 0.03, blue: 0.07)],
                                     startPoint: .top, endPoint: .bottom))
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .stroke(.white.opacity(0.12), lineWidth: 6)
            // Siri-style ribbons in the three mode colors, frozen mid-phrase.
            ZStack {
                Ribbons(palette: Palette(a: Color(red: 1.0, green: 0.30, blue: 0.45),
                                         b: Color(red: 0.30, green: 0.55, blue: 1.0),
                                         c: Color(red: 0.55, green: 0.40, blue: 1.0)), level: 0.9, phase: .listening)
            }
            .frame(width: 620, height: 420)
        }
        .frame(width: 824, height: 824)
        .shadow(color: .black.opacity(0.35), radius: 24, y: 12)
        .frame(width: 1024, height: 1024)
    }
}

/// The Prometheus Swarm icon: the same dark tile, but a network instead of a voice: you in the middle, your computers around you.
struct SwarmIconView: View {
    private let teal = Color(red: 0.20, green: 0.83, blue: 0.60)
    private let violet = Color(red: 0.55, green: 0.40, blue: 1.0)
    private let nodes: [CGPoint] = [CGPoint(x: -250, y: -105), CGPoint(x: 0, y: -230), CGPoint(x: 250, y: -105),
                                    CGPoint(x: -190, y: 175), CGPoint(x: 190, y: 175)]

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.06, green: 0.13, blue: 0.17), Color(red: 0.02, green: 0.04, blue: 0.07)],
                                     startPoint: .top, endPoint: .bottom))
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .stroke(.white.opacity(0.12), lineWidth: 6)
            ZStack {
                ZStack {                                                                  // links, from the middle of the tile
                    ForEach(0..<nodes.count, id: \.self) { i in
                        Path { p in
                            p.move(to: CGPoint(x: 412, y: 412))
                            p.addLine(to: CGPoint(x: 412 + nodes[i].x, y: 412 + nodes[i].y))
                        }
                        .stroke(LinearGradient(colors: [violet, teal.opacity(0.8)], startPoint: .top, endPoint: .bottom),
                                style: StrokeStyle(lineWidth: 9, lineCap: .round, dash: i % 2 == 0 ? [] : [2, 22]))
                    }
                }
                .frame(width: 824, height: 824)
                ForEach(0..<nodes.count, id: \.self) { i in                              // your other computers
                    ZStack {
                        Circle().fill(teal.opacity(0.18)).frame(width: 118, height: 118).blur(radius: 14)
                        Circle().fill(Color(red: 0.04, green: 0.08, blue: 0.11)).frame(width: 92, height: 92)
                        Circle().stroke(teal, lineWidth: 9).frame(width: 92, height: 92)
                        Circle().fill(teal).frame(width: 26, height: 26)
                    }
                    .offset(x: nodes[i].x, y: nodes[i].y)
                }
                ZStack {                                                                  // you
                    Circle().fill(violet.opacity(0.55)).frame(width: 250, height: 250).blur(radius: 38)
                    Circle().fill(RadialGradient(colors: [.white, Color(red: 0.80, green: 0.65, blue: 1.0), violet], center: .center,
                                                 startRadius: 4, endRadius: 80)).frame(width: 150, height: 150)
                    Circle().stroke(.white.opacity(0.85), lineWidth: 5).frame(width: 150, height: 150)
                }
            }
            .offset(y: -8)
        }
        .frame(width: 824, height: 824)
        .shadow(color: .black.opacity(0.35), radius: 24, y: 12)
        .frame(width: 1024, height: 1024)
    }
}

/// The Prometheus Switch icon: the two apps, and the swap between them.
struct SwitchIconView: View {
    /// Five small computers around the middle of the swarm circle.
    private var dots: [CGPoint] {
        (0..<5).map { i in
            let angle: Double = Double(i) * 2.0 * Double.pi / 5.0 - Double.pi / 2.0
            return CGPoint(x: CGFloat(cos(angle)) * 78, y: CGFloat(sin(angle)) * 78)
        }
    }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 185, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.11, green: 0.12, blue: 0.18), Color(red: 0.03, green: 0.04, blue: 0.08)], startPoint: .top, endPoint: .bottom))
            RoundedRectangle(cornerRadius: 185, style: .continuous).stroke(.white.opacity(0.12), lineWidth: 6)
            ZStack {
                ZStack {                                                              // Voice Control: its ribbons, in a circle
                    Circle().fill(Color(red: 0.07, green: 0.07, blue: 0.14)).frame(width: 250, height: 250)
                    Circle().stroke(LinearGradient(colors: [Color(red: 1.0, green: 0.30, blue: 0.45), Color(red: 0.55, green: 0.40, blue: 1.0)],
                                                   startPoint: .top, endPoint: .bottom), lineWidth: 12).frame(width: 250, height: 250)
                    Ribbons(palette: Palette(a: Color(red: 1.0, green: 0.30, blue: 0.45), b: Color(red: 0.30, green: 0.55, blue: 1.0),
                                             c: Color(red: 0.55, green: 0.40, blue: 1.0)), level: 0.9, phase: .listening)
                        .frame(width: 300, height: 200).scaleEffect(0.62).clipShape(Circle().inset(by: 14)).frame(width: 250, height: 250)
                }
                .offset(x: -215)
                ZStack {                                                              // Prometheus Swarm: you and your computers, in a circle
                    Circle().fill(Color(red: 0.04, green: 0.09, blue: 0.12)).frame(width: 250, height: 250)
                    Circle().stroke(Color(red: 0.20, green: 0.83, blue: 0.60), lineWidth: 12).frame(width: 250, height: 250)
                    Circle().fill(Color(red: 0.55, green: 0.40, blue: 1.0)).frame(width: 62, height: 62)
                    ForEach(0..<dots.count, id: \.self) { i in
                        Circle().fill(Color(red: 0.20, green: 0.83, blue: 0.60)).frame(width: 24, height: 24).offset(x: dots[i].x, y: dots[i].y)
                    }
                }
                .offset(x: 215)
                Image(systemName: "arrow.left.arrow.right").font(.system(size: 130, weight: .bold)).foregroundStyle(.white)
            }
        }
        .frame(width: 824, height: 824)
        .shadow(color: .black.opacity(0.35), radius: 24, y: 12)
        .frame(width: 1024, height: 1024)
    }
}

/// `VoiceOverlay --icon <out.png> [--swarm | --switch]` renders the 1024px app icon.
@MainActor
func renderIcon(to path: String, swarm: Bool = false, switcher: Bool = false) {
    let r = ImageRenderer(content: Group { if switcher { SwitchIconView() } else if swarm { SwarmIconView() } else { AppIconView() } })
    r.scale = 1
    guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { print("icon render failed"); return }
    try? png.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}
