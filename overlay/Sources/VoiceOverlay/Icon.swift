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

/// `VoiceOverlay --icon <out.png>` renders the 1024px app icon.
@MainActor
func renderIcon(to path: String) {
    let r = ImageRenderer(content: AppIconView())
    r.scale = 1
    guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { print("icon render failed"); return }
    try? png.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}
