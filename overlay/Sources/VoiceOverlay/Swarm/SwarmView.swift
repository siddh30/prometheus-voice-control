import SwiftUI

private let accent = Color(red: 0.55, green: 0.40, blue: 1.0)
private let ready = Color(red: 0.20, green: 0.83, blue: 0.60)

/// The floating Prometheus Swarm widget: a small pill at rest, the full card (devices, input, live work) when opened or working.
struct SwarmWidget: View {
    @ObservedObject var model: SwarmModel
    var onSize: (CGSize) -> Void = { _ in }

    var body: some View {
        Group {
            if model.expanded || model.isBusy { SwarmCard(model: model) } else { SwarmPill(model: model) }
        }
        .background(GeometryReader { g in Color.clear.preference(key: SizeKey.self, value: g.size) })
        .onPreferenceChange(SizeKey.self) { onSize($0) }
        .padding(16)
        .animation(.spring(response: 0.4, dampingFraction: 0.82), value: model.expanded)
        .animation(.spring(response: 0.4, dampingFraction: 0.82), value: model.isBusy)
    }
}

/// At rest: SWARM, a dot per computer (green ready, purple working, grey offline), and a count. Click to open.
struct SwarmPill: View {
    @ObservedObject var model: SwarmModel
    var body: some View {
        HStack(spacing: 9) {
            SwarmTag()
            HStack(spacing: 5) { ForEach(model.devices) { d in Circle().fill(color(d, model.working.contains(d.name))).frame(width: 7, height: 7) } }
            Text(model.stateLabel).font(.system(size: 10, weight: .semibold, design: .rounded)).tracking(2.0)
                .foregroundStyle(.white.opacity(model.connected ? 0.7 : 0.4)).lineLimit(1).fixedSize()
            Image(systemName: "chevron.up").font(.system(size: 8, weight: .bold)).foregroundStyle(.white.opacity(0.4))
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(Capsule().fill(Color(red: 0.08, green: 0.09, blue: 0.10).opacity(0.93))
            .overlay(Capsule().stroke(.white.opacity(0.12), lineWidth: 0.6)))
        .contentShape(Capsule())
        .onTapGesture { model.expanded = true }
    }
}

struct SwarmTag: View {
    var body: some View {
        Text("SWARM").font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(Capsule().fill(accent.opacity(0.25))).foregroundStyle(accent)
    }
}

func color(_ d: SwarmDevice, _ working: Bool) -> Color { !d.online ? .white.opacity(0.28) : working ? accent : ready }

struct SwarmCard: View {
    @ObservedObject var model: SwarmModel
    @StateObject private var speech = SwarmSpeech()
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 10) {
            Ribbons(palette: speech.listening ? .listening : .agent, level: speech.level,
                    phase: speech.listening ? .listening : model.isWorking ? .acting : model.pending.isEmpty ? .idle : .thinking).frame(height: 56)

            HStack(spacing: 8) {
                SwarmTag()
                if model.isBusy { Spinner(color: accent) }
                Text(speech.listening ? "LISTENING" : model.stateLabel).font(.system(size: 10, weight: .semibold, design: .rounded)).tracking(2.2)
                    .foregroundStyle(model.connected ? Color.white.opacity(model.isBusy ? 0.92 : 0.5) : Color.orange.opacity(0.9)).lineLimit(1).fixedSize()
                Button { model.expanded = false; model.typing = false } label: {
                    Image(systemName: "chevron.down").font(.system(size: 9, weight: .bold)).foregroundStyle(.white.opacity(0.55))
                        .frame(width: 22, height: 22).background(Circle().fill(.white.opacity(0.08)))
                }.buttonStyle(.plain).help("Minimise")
            }
            .padding(.horizontal, 14).padding(.vertical, 6)
            .background(Capsule().fill(.white.opacity(0.07)).overlay(Capsule().stroke(.white.opacity(0.10), lineWidth: 0.5)))

            DeviceMap(devices: model.devices, working: model.working).frame(height: 128)

            HStack(spacing: 8) {
                TextField(speech.listening ? "Listening…" : "Tell the swarm what to do…", text: $model.draft)
                    .textFieldStyle(.plain).font(.system(size: 13, design: .rounded)).foregroundStyle(.white)
                    .focused($focused)
                    .onSubmit { send() }
                    .onChange(of: model.draft) { _ in model.touch() }
                    .onExitCommand { model.typing = false; model.expanded = false }
                Button { speak() } label: {
                    Image(systemName: speech.listening ? "stop.fill" : "mic.fill").font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(speech.listening ? Color.white : .white.opacity(0.7))
                        .frame(width: 28, height: 28)
                        .background(Circle().fill(speech.listening ? Color(red: 1.0, green: 0.30, blue: 0.40) : .white.opacity(0.10)))
                }.buttonStyle(.plain).help(speech.listening ? "Stop listening" : "Speak to the swarm")
                Button { send() } label: {
                    Text("Go").font(.system(size: 12, weight: .bold, design: .rounded)).foregroundStyle(.white)
                        .padding(.horizontal, 14).padding(.vertical, 6).background(Capsule().fill(accent))
                }.buttonStyle(.plain)
            }
            .padding(.leading, 12).padding(.trailing, 6).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.white.opacity(0.10)))
            .onAppear { model.typing = true; speech.problem = { model.say($0) }; DispatchQueue.main.async { focused = true } }
            .onDisappear { model.typing = false }
            .onChange(of: speech.heard) { if speech.listening { model.draft = $0 } }

            if !model.notice.isEmpty {
                Text(model.notice).font(.system(size: 10.5, design: .rounded)).foregroundStyle(.orange.opacity(0.9))
            } else if model.devices.count > 1 {
                Text("Add “on the mini” to choose a computer").font(.system(size: 9.5, design: .rounded)).foregroundStyle(.white.opacity(0.38))
            }

            ForEach(model.pending) { PendingCard(request: $0) }
            if let job = model.latestJob { JobCard(job: job, model: model) }
            if model.moreRunning > 0 {
                Text("+\(model.moreRunning) more running").font(.system(size: 10, weight: .semibold, design: .rounded)).foregroundStyle(accent.opacity(0.8))
            }
        }
        .padding(.horizontal, 20).padding(.vertical, 16)
        .frame(width: 440)
        .onHover { model.hovering = $0; if $0 { model.touch() } }
        .background(
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).fill(Color(red: 0.08, green: 0.09, blue: 0.10).opacity(0.93)))
                .overlay(RoundedRectangle(cornerRadius: 26, style: .continuous).stroke(accent.opacity(model.isWorking ? 0.6 : 0.35), lineWidth: 1.2))
        )
    }
}

extension SwarmCard {
    func send() {
        model.touch()
        let t = model.draft
        model.draft = ""
        model.submit(t)
    }
    func speak() {
        model.touch(); model.expanded = true
        speech.toggle { text in model.draft = ""; model.submit(text) }
    }
}

/// You in the middle, your computers around you. A link lights up and its dashes flow while that computer is working.
struct DeviceMap: View {
    let devices: [SwarmDevice]
    let working: Set<String>

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            let me = CGPoint(x: devices.count == 1 ? w * 0.34 : w / 2, y: h * 0.42)
            let spots = layout(devices.count, w: w, h: h, me: me)
            TimelineView(.animation) { tl in
                let t = tl.date.timeIntervalSinceReferenceDate
                ZStack {
                    ForEach(Array(devices.enumerated()), id: \.element.id) { i, d in
                        let on = working.contains(d.name)
                        Path { p in p.move(to: me); p.addLine(to: spots[i]) }
                            .stroke(on ? accent : .white.opacity(d.online ? 0.16 : 0.07),
                                    style: StrokeStyle(lineWidth: on ? 2 : 1.4, dash: on ? [6, 8] : d.online ? [] : [3, 6], dashPhase: on ? CGFloat(-t * 30) : 0))
                    }
                    Orb(label: "YOU", sub: "THIS MAC", ring: accent, glyph: nil, pulse: false, t: t).position(me)
                    ForEach(Array(devices.enumerated()), id: \.element.id) { i, d in
                        let on = working.contains(d.name)
                        Orb(label: d.name, sub: !d.online ? "OFFLINE" : on ? "WORKING" : "READY", ring: color(d, on),
                            glyph: glyph(d.name), pulse: on, t: t, dashed: !d.online).opacity(d.online ? 1 : 0.55).position(spots[i])
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
    var body: some View {
        VStack(spacing: 3) {
            ZStack {
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

struct JobCard: View {
    let job: SwarmJob
    @ObservedObject var model: SwarmModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(job.state == "running" ? "IN PROGRESS" : job.state.uppercased()).font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6)
                    .foregroundStyle(.white.opacity(0.5))
                Spacer()
                if job.state == "running" {
                    Button { model.cancel(job.id) } label: {
                        Text("Stop").font(.system(size: 11, weight: .bold, design: .rounded)).foregroundStyle(Color(red: 1, green: 0.45, blue: 0.45))
                            .padding(.horizontal, 12).padding(.vertical, 4).background(Capsule().fill(Color.red.opacity(0.2)))
                    }.buttonStyle(.plain)
                }
            }
            Text("“\(job.request)”").font(.system(size: 13, design: .rounded)).foregroundStyle(.white.opacity(0.92)).fixedSize(horizontal: false, vertical: true)
            ForEach(job.subs) { SubRow(sub: $0, model: model) }
            if job.subs.count > 1, !job.result.isEmpty {
                Divider().overlay(.white.opacity(0.1))
                Text(job.result).font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(0.92)).lineLimit(6)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled).help(job.result)
            } else if job.subs.isEmpty, !job.result.isEmpty {
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
                if !sub.why.isEmpty { Text(sub.why).font(.system(size: 10.5, design: .rounded)).foregroundStyle(.white.opacity(0.45)) }
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
                    Text(c.text).font(.system(size: 11.5, weight: c.status == "in_progress" ? .semibold : .regular, design: .rounded))
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
            if !sub.result.isEmpty, sub.state != "running" {
                Text(sub.result).font(.system(size: 12, design: .rounded)).foregroundStyle(.white.opacity(0.9)).lineLimit(6)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled).padding(.leading, 14).help(sub.result)
            }
        }
    }
    private var dot: Color {
        switch sub.state { case "running": return accent; case "done": return .green; case "failed", "cancelled": return .red; default: return .white.opacity(0.4) }
    }
    private func line(_ s: String, _ c: Color) -> some View { Text(s).font(.system(size: 10.5, design: .rounded)).foregroundStyle(c).padding(.leading, 14) }
}


/// Shown the moment you press Go, until the hub has picked the request up (it is deciding who should do it).
struct PendingCard: View {
    let request: SwarmPending
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Spinner(color: accent)
                Text("DECIDING WHO SHOULD DO THIS").font(.system(size: 9, weight: .bold, design: .rounded)).tracking(1.6).foregroundStyle(.white.opacity(0.55))
            }
            Text("“\(request.text)”").font(.system(size: 13, design: .rounded)).foregroundStyle(.white.opacity(0.92)).fixedSize(horizontal: false, vertical: true)
            Shimmer().frame(height: 6).clipShape(Capsule())
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(12)
        .background(RoundedRectangle(cornerRadius: 16).fill(.black.opacity(0.28)))
        .transition(.opacity)
    }
}

/// A soft light sweeping across a bar: "something is happening".
struct Shimmer: View {
    var body: some View {
        TimelineView(.animation) { tl in
            let t = tl.date.timeIntervalSinceReferenceDate
            GeometryReader { g in
                let x = (t * 0.8).truncatingRemainder(dividingBy: 1)
                ZStack(alignment: .leading) {
                    Rectangle().fill(.white.opacity(0.07))
                    Rectangle().fill(LinearGradient(colors: [.clear, accent.opacity(0.8), .clear], startPoint: .leading, endPoint: .trailing))
                        .frame(width: g.size.width * 0.4).offset(x: (g.size.width * 1.4) * x - g.size.width * 0.4)
                }
            }
        }
    }
}
