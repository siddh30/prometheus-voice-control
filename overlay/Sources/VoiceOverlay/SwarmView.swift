import SwiftUI

// The swarm in the widget: a map of your computers you can tap to pick, and a box that shows every computer's progress.
// Both draw from one `SwarmBoard` that the daemon publishes (the `swarm` event); nothing here waits on a computer.

enum SwarmStyle {
    static let accent = Color(red: 0.55, green: 0.40, blue: 1.0)      // working, picked
    static let ready = Color(red: 0.20, green: 0.83, blue: 0.60)      // online and free, or done
    static let warn = Color(red: 1.0, green: 0.62, blue: 0.20)        // stalled, not responding, waiting for you
    static let bad = Color(red: 1.0, green: 0.35, blue: 0.35)         // failed
    static let off = Color.white.opacity(0.28)                        // offline
}

struct SwarmPC: Identifiable, Equatable {
    var id: String { name }
    let name: String
    let local: Bool
    let state: String          // idle | working | waiting | stalled | done | failed | connecting | not_responding | offline
    let detail: String
    let task: String
    let checklist: [ChecklistItem]
    let step: String
    let result: String
    let error: String
    let approval: String

    static func == (a: SwarmPC, b: SwarmPC) -> Bool {
        a.name == b.name && a.state == b.state && a.task == b.task && a.step == b.step && a.result == b.result && a.error == b.error
            && a.detail == b.detail && a.approval == b.approval && a.checklist.map(\.text) == b.checklist.map(\.text) && a.checklist.map(\.status) == b.checklist.map(\.status)
    }

    var busy: Bool { state == "working" || state == "waiting" || state == "stalled" }
    var problem: Bool { ["offline", "not_responding", "connecting", "failed", "stalled"].contains(state) }
    var color: Color {
        switch state {
        case "working": return SwarmStyle.accent
        case "waiting", "stalled", "not_responding": return SwarmStyle.warn
        case "failed": return SwarmStyle.bad
        case "offline", "connecting": return SwarmStyle.off
        default: return SwarmStyle.ready
        }
    }
    var label: String {
        switch state {
        case "working": return "WORKING"
        case "waiting": return "WAITING FOR YOU"
        case "stalled": return "QUIET, STILL WORKING"
        case "not_responding": return "NOT RESPONDING"
        case "offline": return "OFFLINE"
        case "connecting": return "CONNECTING"
        case "done": return "DONE"
        case "failed": return "FAILED"
        default: return "READY"
        }
    }
    /// Shown in the box: the computer has something to say (work in progress, a result, or a problem).
    var inBox: Bool { !task.isEmpty || problem }
}

struct SwarmBoard: Equatable {
    var pcs: [SwarmPC]
    var picked: String
    var routing: String

    init?(_ data: [String: Any]) {
        guard let raw = data["pcs"] as? [[String: Any]] else { return nil }
        pcs = raw.map { p in
            let items = (p["checklist"] as? [[String: Any]] ?? []).map { ChecklistItem(text: $0["text"] as? String ?? "", status: $0["status"] as? String ?? "pending") }
            return SwarmPC(name: p["name"] as? String ?? "", local: p["local"] as? Bool ?? false, state: p["state"] as? String ?? "idle",
                           detail: p["detail"] as? String ?? "", task: p["task"] as? String ?? "", checklist: items,
                           step: p["step"] as? String ?? "", result: p["result"] as? String ?? "", error: p["error"] as? String ?? "",
                           approval: p["approval"] as? String ?? "")
        }
        picked = data["picked"] as? String ?? "auto"
        routing = data["routing"] as? String ?? ""
    }

    var me: SwarmPC? { pcs.first(where: { $0.local }) }
    var others: [SwarmPC] { pcs.filter { !$0.local } }
    var anyBusy: Bool { pcs.contains(where: { $0.busy }) }
    var anyInBox: Bool { pcs.contains(where: { $0.inBox }) }
}

/// You in the middle, your other computers around you. A link lights up and its dashes flow while that computer works;
/// tap a computer to send your requests there, tap it again (or AUTO) to let the router decide.
/// Drawn in ONE Canvas, like the ribbons (many animated views would be redrawn and diffed 144 times a second), and only at
/// the display's full rate while a computer is working; when nothing moves it redraws once a second. The motion runs from its
/// own clock: nothing in it waits for the daemon.
struct SwarmMap: View {
    let board: SwarmBoard
    var onPick: (String) -> Void

    private var tall: Bool { board.others.count >= 3 }

    var body: some View {
        VStack(spacing: 4) {
            GeometryReader { geo in
                let spots = Self.layout(board.others.count, size: geo.size)
                ZStack {
                    if board.anyBusy {
                        TimelineView(.animation) { tl in canvas(spots, t: tl.date.timeIntervalSinceReferenceDate) }
                    } else {
                        TimelineView(.periodic(from: .now, by: 1)) { tl in canvas(spots, t: tl.date.timeIntervalSinceReferenceDate) }
                    }
                    // The Canvas only draws; taps and tooltips live on still, invisible areas over each orb.
                    tapArea(at: spots.me) { if let m = board.me { onPick(board.picked == m.name ? "auto" : m.name) } }
                    ForEach(Array(board.others.enumerated()), id: \.element.id) { i, pc in
                        tapArea(at: spots.others[i], help: pc.state == "offline" ? (pc.detail.isEmpty ? "Offline" : pc.detail) : "Send your requests to this computer") {
                            if pc.state != "offline" { onPick(board.picked == pc.name ? "auto" : pc.name) }
                        }
                    }
                }
            }
            .frame(height: tall ? 215 : 150)
            Text("AUTO")
                .font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
                .padding(.horizontal, 9).padding(.vertical, 3)
                .background(Capsule().fill(board.picked == "auto" ? SwarmStyle.accent.opacity(0.30) : Color.white.opacity(0.07)))
                .foregroundStyle(board.picked == "auto" ? SwarmStyle.accent : Color.white.opacity(0.5))
                .onTapGesture { onPick("auto") }
                .help("Let the router decide which computer gets each request")
        }
    }

    private func tapArea(at p: CGPoint, help: String = "Send your requests to this computer", _ action: @escaping () -> Void) -> some View {
        Color.clear.frame(width: 96, height: 96).contentShape(Rectangle()).position(x: p.x, y: p.y + 14).onTapGesture(perform: action).help(help)
    }

    // ---- the picture ----
    private func canvas(_ spots: (me: CGPoint, others: [CGPoint]), t: Double) -> some View {
        Canvas { ctx, _ in
            for (i, pc) in board.others.enumerated() { drawLink(&ctx, pc, from: spots.me, to: spots.others[i], t: t) }
            drawOrb(&ctx, at: spots.me, label: nil, sub: board.me.map { $0.busy ? "WORKING" : board.picked == $0.name ? "SENDING HERE" : "THIS MAC" } ?? "THIS MAC",
                    ring: board.me?.color ?? SwarmStyle.accent, glyph: nil, pulse: board.me?.busy ?? false,
                    selected: board.me.map { board.picked == $0.name } ?? false, dashed: false, t: t)
            for (i, pc) in board.others.enumerated() {
                drawOrb(&ctx, at: spots.others[i], label: pc.name, sub: board.picked == pc.name && pc.state == "idle" ? "SENDING HERE" : pc.label, ring: pc.color,
                        glyph: glyph(pc.name), pulse: pc.busy, selected: board.picked == pc.name,
                        dashed: pc.state == "offline" || pc.state == "connecting", t: t, faded: pc.state == "offline")
            }
        }
    }

    private func drawLink(_ ctx: inout GraphicsContext, _ pc: SwarmPC, from a: CGPoint, to b: CGPoint, t: Double) {
        let flowing = pc.state == "working"
        let stalled = pc.state == "stalled" || pc.state == "waiting" || pc.state == "not_responding"
        let dx = b.x - a.x, dy = b.y - a.y, len = max(1, (dx * dx + dy * dy).squareRoot())
        var path = Path()                                                           // stops at the orbs' edges, never through a label
        path.move(to: CGPoint(x: a.x + dx / len * 30, y: a.y + dy / len * 30))
        path.addLine(to: CGPoint(x: b.x - dx / len * 30, y: b.y - dy / len * 30))
        let color: Color = flowing ? SwarmStyle.accent : stalled ? SwarmStyle.warn.opacity(0.8) : .white.opacity(pc.state == "offline" ? 0.07 : 0.16)
        let dash: [CGFloat] = flowing ? [6, 8] : pc.state == "offline" ? [3, 6] : stalled ? [4, 6] : []
        ctx.stroke(path, with: .color(color), style: StrokeStyle(lineWidth: flowing ? 2 : 1.4, dash: dash, dashPhase: flowing ? CGFloat(-t * 30) : 0))
    }

    private func drawOrb(_ ctx: inout GraphicsContext, at p: CGPoint, label: String?, sub: String, ring: Color, glyph: String?, pulse: Bool,
                         selected: Bool, dashed: Bool, t: Double, faded: Bool = false) {
        func circle(_ r: CGFloat) -> Path { Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2)) }
        if faded { ctx.opacity = 0.55 }
        if selected {
            ctx.fill(circle(31), with: .color(SwarmStyle.accent.opacity(0.16)))
            ctx.stroke(circle(31), with: .color(SwarmStyle.accent), style: StrokeStyle(lineWidth: 2.5, dash: [3, 4]))
        }
        if pulse {
            let k = (t * 0.9).truncatingRemainder(dividingBy: 1)
            ctx.stroke(circle(24 + 12 * CGFloat(k)), with: .color(ring.opacity(0.5 * (1 - k))), lineWidth: 2)
        }
        ctx.fill(circle(24), with: .color(Color(red: 0.08, green: 0.09, blue: 0.10)))
        ctx.fill(circle(24), with: .color(glyph == nil ? SwarmStyle.accent.opacity(0.20) : Color.white.opacity(0.05)))
        ctx.stroke(circle(24), with: .color(ring), style: StrokeStyle(lineWidth: 2.2, dash: dashed ? [4, 4] : []))
        if let g = glyph {
            var image = ctx.resolve(Image(systemName: g))
            image.shading = .color(ring)
            ctx.draw(image, in: CGRect(x: p.x - 11, y: p.y - 9, width: 22, height: 18))
        } else {
            ctx.draw(ctx.resolve(Text("YOU").font(.system(size: 10, weight: .bold, design: .rounded)).foregroundColor(.white)), at: p)
        }
        var y = p.y + 38
        if let label {
            ctx.draw(ctx.resolve(Text(label).font(.system(size: 11, weight: .semibold, design: .rounded)).foregroundColor(.white)), at: CGPoint(x: p.x, y: y))
            y += 14
        }
        ctx.draw(ctx.resolve(Text(sub).font(.system(size: 8, weight: .bold, design: .rounded)).tracking(1.1).foregroundColor(ring.opacity(0.9))), at: CGPoint(x: p.x, y: y))
        ctx.opacity = 1
    }

    /// One or two computers sit beside you; three or more fan out in an arc over the top, so none can land on "YOU".
    static func layout(_ n: Int, size: CGSize) -> (me: CGPoint, others: [CGPoint]) {
        let w = size.width, h = size.height
        let crowded = n >= 3
        let me = CGPoint(x: n == 1 ? w * 0.34 : w / 2, y: crowded ? h * 0.62 : h * 0.38)
        switch n {
        case 0: return (me, [])
        case 1: return (me, [CGPoint(x: w * 0.68, y: me.y)])
        case 2: return (me, [CGPoint(x: w * 0.14, y: me.y), CGPoint(x: w * 0.86, y: me.y)])
        default:
            return (me, (0..<n).map { i in
                let a = Double.pi + Double.pi * Double(i) / Double(n - 1)                   // left, over the top, right
                return CGPoint(x: me.x + CGFloat(cos(a)) * w * 0.39, y: me.y + CGFloat(sin(a)) * h * 0.38)
            })
        }
    }

    private func glyph(_ name: String) -> String {
        let n = name.lowercased()
        return (n.contains("mini") || n.contains("studio") || n.contains("imac") || n.contains("desktop")) ? "desktopcomputer" : "laptopcomputer"
    }
}

/// The black box: everything each computer is doing, with tick marks, and nothing else. No second "final answer" line:
/// a computer's result is the last line under its own name.
struct SwarmBox: View {
    let board: SwarmBoard

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(board.pcs.filter { $0.inBox }) { pc in
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 6) {
                        Circle().fill(pc.color).frame(width: 7, height: 7)
                        Text(pc.name.uppercased()).font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.4).foregroundStyle(.white.opacity(0.75))
                        Text(pc.label).font(.system(size: 8, weight: .bold, design: .rounded)).tracking(1.0).foregroundStyle(pc.color)
                        Spacer(minLength: 0)
                    }
                    if !pc.task.isEmpty {
                        Text(pc.task).font(.system(size: 11, design: .rounded)).foregroundStyle(.white.opacity(0.5)).lineLimit(2)
                    }
                    ForEach(Array(pc.checklist.enumerated()), id: \.offset) { _, item in
                        HStack(alignment: .top, spacing: 8) {
                            switch item.status {
                            case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
                            case "in_progress": Spinner(color: SwarmStyle.accent)
                            case "skipped": Image(systemName: "minus.circle").foregroundStyle(.white.opacity(0.35))
                            default: Image(systemName: "circle").foregroundStyle(.white.opacity(0.35))
                            }
                            Text(item.text).font(.system(size: 12, weight: item.status == "in_progress" ? .semibold : .regular, design: .rounded))
                                .foregroundStyle(.white.opacity(item.status == "done" || item.status == "skipped" ? 0.5 : 0.95))
                                .strikethrough(item.status == "skipped").lineLimit(2)
                        }
                        .font(.system(size: 12))
                    }
                    if !pc.step.isEmpty {
                        HStack(spacing: 5) {
                            Image(systemName: "arrow.right").font(.system(size: 8, weight: .bold))
                            Text(pc.step).lineLimit(1).truncationMode(.middle)
                        }
                        .font(.system(size: 10, design: .rounded)).foregroundStyle(SwarmStyle.accent.opacity(0.9))
                    }
                    if !pc.approval.isEmpty {
                        Text(pc.approval).font(.system(size: 11, weight: .medium, design: .rounded)).foregroundStyle(SwarmStyle.warn).lineLimit(3)
                    }
                    if !pc.result.isEmpty {
                        Text(pc.result).font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(0.92)).lineLimit(6)
                    }
                    if !pc.error.isEmpty {
                        Text(pc.error).font(.system(size: 11, design: .rounded)).foregroundStyle(SwarmStyle.bad).lineLimit(3)
                    } else if pc.state == "offline" || pc.state == "not_responding" {
                        Text(pc.detail.isEmpty ? pc.label.capitalized : pc.detail).font(.system(size: 11, design: .rounded)).foregroundStyle(SwarmStyle.warn).lineLimit(2)
                    }
                }
                if pc.id != board.pcs.filter({ $0.inBox }).last?.id { Divider().overlay(.white.opacity(0.08)) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16).fill(.black.opacity(0.25)))
    }
}

/// Opens the map without pressing F10, so you can pick a computer first.
struct NetworkButton: View {
    let on: Bool
    let action: () -> Void
    var body: some View {
        Image(systemName: "point.3.connected.trianglepath.dotted")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(on ? Color.white : Color.white.opacity(0.6))
            .frame(width: 22, height: 22)
            .background(Circle().fill(on ? SwarmStyle.accent.opacity(0.45) : Color.white.opacity(0.08)))
            .contentShape(Circle())
            .onTapGesture(perform: action)
            .help("Show your computers")
    }
}
