import SwiftUI
import AppKit

/// `PrometheusSwitch --snapshot <dir>`: renders the widget's states to PNG files, to look at them without clicking through the app.
@MainActor
func renderSnapshots(to dir: String) {
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    func shot(_ name: String, _ setup: (SwitchModel) -> Void) {
        let model = SwitchModel()
        setup(model)
        let size = WidgetSize.card
        let view = SwitchWidget(model: model).padding(20).background(Color(white: 0.55)).frame(width: size.width + 40, height: size.height + 40)
        let r = ImageRenderer(content: view)
        r.scale = 2
        guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { print("fail \(name)"); return }
        try? png.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
        print("wrote \(name).png")
    }
    shot("1_card_single_running") { $0.expanded = true; $0.running = [.single] }
    shot("2_card_switching") { $0.expanded = true; $0.running = [.single]; $0.busy = true; $0.message = "Closing Voice Control…" }
    shot("3_card_failed") { $0.expanded = true; $0.running = [.swarm]; $0.failed = true; $0.message = "Prometheus Swarm didn't quit. Quit it yourself, then try again." }
}
