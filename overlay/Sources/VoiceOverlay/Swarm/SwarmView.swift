import SwiftUI

let accent = Color(red: 0.55, green: 0.40, blue: 1.0)
let ready = Color(red: 0.20, green: 0.83, blue: 0.60)

func color(_ d: SwarmDevice, _ working: Bool) -> Color { !d.online ? .white.opacity(0.28) : working ? accent : ready }

/// You in the middle, your computers around you. A link lights up and its dashes flow while that computer is working.
struct DeviceMap: View {
    let devices: [SwarmDevice]
    let working: Set<String>
    var selected: Set<String> = []
    var youWorking = false                      // this computer's own assistant is working (no other computer is): same pulse as any other
    var onTap: ((String) -> Void)? = nil

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            let me = CGPoint(x: devices.count == 1 ? w * 0.34 : w / 2, y: h * 0.42)
            let spots = layout(devices.count, w: w, h: h, me: me)
            TimelineView(working.isEmpty ? AnimationTimelineSchedule(minimumInterval: 5, paused: UIMotion.calm) : UIMotion.full) { tl in
                let t = tl.date.timeIntervalSinceReferenceDate
                ZStack {
                    ForEach(Array(devices.enumerated()), id: \.element.id) { i, d in
                        let on = working.contains(d.name)
                        Path { p in p.move(to: me); p.addLine(to: spots[i]) }
                            .stroke(on ? accent : .white.opacity(d.online ? 0.16 : 0.07),
                                    style: StrokeStyle(lineWidth: on ? 2 : 1.4, dash: on ? [6, 8] : d.online ? [] : [3, 6], dashPhase: on ? CGFloat(-t * 30) : 0))
                    }
                    Orb(label: "YOU", sub: "THIS MAC", ring: accent, glyph: nil, pulse: youWorking, t: t).position(me)
                    ForEach(Array(devices.enumerated()), id: \.element.id) { i, d in
                        let on = working.contains(d.name)
                        Orb(label: d.name, sub: !d.online ? "OFFLINE" : on ? "WORKING" : selected.contains(d.name) ? "SENDING HERE" : "READY", ring: color(d, on),
                            glyph: glyph(d.name), pulse: on, t: t, dashed: !d.online, selected: selected.contains(d.name))
                            .opacity(d.online ? 1 : 0.55).position(spots[i])
                            .onTapGesture { if d.online { onTap?(d.name) } }
                            .help(d.online ? "Click to send your requests to this computer" : "Offline")
                    }
                    if devices.isEmpty {
                        Text("No computers have joined yet").font(.system(size: 11, design: .rounded)).foregroundStyle(.white.opacity(0.45)).position(x: w / 2, y: h * 0.8)
                    }
                }
            }
        }
    }

    private func layout(_ n: Int, w: CGFloat, h: CGFloat, me: CGPoint) -> [CGPoint] {
        switch n {
        case 0: return []
        case 1: return [CGPoint(x: w * 0.68, y: me.y)]
        case 2: return [CGPoint(x: w * 0.14, y: me.y), CGPoint(x: w * 0.86, y: me.y)]
        default:
            return (0..<n).map { i in
                let a = -Double.pi / 2 + 2 * Double.pi * (Double(i) + 0.5) / Double(n)
                return CGPoint(x: me.x + CGFloat(cos(a)) * w * 0.38, y: me.y + CGFloat(sin(a)) * h * 0.30)
            }
        }
    }
    private func glyph(_ name: String) -> String {
        let n = name.lowercased()
        return (n.contains("mini") || n.contains("studio") || n.contains("imac") || n.contains("desktop")) ? "desktopcomputer" : "laptopcomputer"
    }
}

struct Orb: View {
    let label: String, sub: String, ring: Color
    let glyph: String?
    let pulse: Bool
    let t: Double
    var dashed = false
    var selected = false
    var body: some View {
        VStack(spacing: 3) {
            ZStack {
                if selected {
                    Circle().stroke(accent, style: StrokeStyle(lineWidth: 2.5, dash: [3, 4])).frame(width: 66, height: 66)
                    Circle().fill(accent.opacity(0.16)).frame(width: 66, height: 66)
                }
                if pulse {
                    let k = (t * 0.9).truncatingRemainder(dividingBy: 1)
                    Circle().stroke(ring.opacity(0.5 * (1 - k)), lineWidth: 2).frame(width: 52 + 26 * k, height: 52 + 26 * k)
                }
                Circle().fill(Color(red: 0.08, green: 0.09, blue: 0.10)).frame(width: 52, height: 52)
                Circle().fill(.white.opacity(glyph == nil ? 0.0 : 0.05)).frame(width: 52, height: 52)
                Circle().fill(accent.opacity(glyph == nil ? 0.20 : 0)).frame(width: 52, height: 52)
                Circle().stroke(ring, style: StrokeStyle(lineWidth: 2.2, dash: dashed ? [4, 4] : [])).frame(width: 52, height: 52)
                if let g = glyph { Image(systemName: g).font(.system(size: 19, weight: .regular)).foregroundStyle(ring) }
                else { Text(label).font(.system(size: 10, weight: .bold, design: .rounded)).foregroundStyle(.white) }
            }
            .frame(width: 78, height: 78)
            if glyph != nil { Text(label).font(.system(size: 11, weight: .semibold, design: .rounded)).foregroundStyle(.white).lineLimit(1) }
            Text(sub).font(.system(size: 8, weight: .bold, design: .rounded)).tracking(1.2).foregroundStyle(.white.opacity(0.5))
        }
        .frame(width: 110)
        .offset(y: glyph == nil ? 8 : 8)
    }
}

/// The plan, and nothing else: which computer is doing what, step by step. Not the request again (you just said it) and not the answer (the
/// assistant says it, once, below). It appears while work is running (or needs you, or failed) and goes away when the work is done.
struct JobCard: View {
    let job: SwarmJob
    @ObservedObject var model: SwarmModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("PLAN").font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6).foregroundStyle(.white.opacity(0.5))
                Spacer()
                if job.state == "running" {
                    Button { model.cancel(job.id) } label: {
                        Text("Stop").font(.system(size: 11, weight: .bold, design: .rounded)).foregroundStyle(Color(red: 1, green: 0.45, blue: 0.45))
                            .padding(.horizontal, 12).padding(.vertical, 4).background(Capsule().fill(Color.red.opacity(0.2)))
                    }.buttonStyle(.plain)
                }
            }
            ForEach(job.subs) { SubRow(sub: $0, model: model) }
            if job.subs.isEmpty, !job.result.isEmpty {
                Text(job.result).font(.system(size: 12, design: .rounded)).foregroundStyle(.orange.opacity(0.9)).fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(12)
        .background(RoundedRectangle(cornerRadius: 16).fill(.black.opacity(0.28)))
    }
}

struct SubRow: View {
    let sub: SwarmSub
    @ObservedObject var model: SwarmModel
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 7) {
                if sub.state == "running" { Spinner(color: accent).scaleEffect(0.8).frame(width: 9, height: 9) } else { Circle().fill(dot).frame(width: 7, height: 7) }
                Text(sub.node).font(.system(size: 12, weight: .bold, design: .rounded)).foregroundStyle(.white)
            }
            if sub.state == "queued" { line("waiting its turn", .white.opacity(0.45)) }
            if sub.state == "running", let now = sub.steps.last(where: { !$0.done }) {
                HStack(spacing: 5) { Image(systemName: "arrow.right").font(.system(size: 8, weight: .bold)); Text(now.text).lineLimit(1).truncationMode(.middle) }
                    .font(.system(size: 10.5, design: .rounded)).foregroundStyle(accent.opacity(0.9)).padding(.leading, 14)
            }
            ForEach(Array(sub.checklist.prefix(6).enumerated()), id: \.offset) { _, c in
                HStack(spacing: 6) {
                    Image(systemName: c.status == "done" ? "checkmark.circle.fill" : c.status == "in_progress" ? "circle.dotted" : "circle")
                        .foregroundStyle(c.status == "done" ? Color.green : c.status == "in_progress" ? accent : .white.opacity(0.35)).font(.system(size: 10))
                    Text(c.text).lineLimit(1).font(.system(size: 11.5, weight: c.status == "in_progress" ? .semibold : .regular, design: .rounded))
                        .foregroundStyle(.white.opacity(c.status == "done" ? 0.5 : 0.92))
                }.padding(.leading, 14)
            }
            if !sub.approval.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text(sub.approval).font(.system(size: 12, weight: .medium, design: .rounded)).foregroundStyle(.white)
                    HStack(spacing: 8) {
                        Button("Deny") { model.approve(sub.id, false) }.buttonStyle(.plain).foregroundStyle(.white.opacity(0.85))
                            .padding(.horizontal, 12).padding(.vertical, 4).background(Capsule().fill(.white.opacity(0.12)))
                        Button("Approve") { model.approve(sub.id, true) }.buttonStyle(.plain).foregroundStyle(.white)
                            .padding(.horizontal, 12).padding(.vertical, 4).background(Capsule().fill(accent))
                    }
                }.padding(10).background(RoundedRectangle(cornerRadius: 10).fill(Color.yellow.opacity(0.14))).padding(.leading, 14)
            }
            if sub.state == "failed", !sub.result.isEmpty {                       // a failure is worth saying; a success is said by the assistant
                Text(sub.result).font(.system(size: 11.5, design: .rounded)).foregroundStyle(.orange.opacity(0.9)).lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, 14)
            }
        }
    }
    private var dot: Color {
        switch sub.state { case "running": return accent; case "done": return .green; case "failed", "cancelled": return .red; default: return .white.opacity(0.4) }
    }
    private func line(_ s: String, _ c: Color) -> some View { Text(s).font(.system(size: 10.5, design: .rounded)).foregroundStyle(c).padding(.leading, 14) }
}
