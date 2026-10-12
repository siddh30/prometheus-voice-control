import SwiftUI
import AppKit

/// `VoiceOverlay --snapshot <dir>` renders each state to a PNG for design review (no screen-recording permission needed).
@MainActor
func renderSnapshots(to dir: String) {
    snapshotFlat = true
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    func shot(_ name: String, _ setup: (OverlayModel) -> Void) {
        let m = OverlayModel()
        m.connected = true
        setup(m)
        let view = OverlayView(model: m)
            .background(ProcessInfo.processInfo.environment["SNAP_BG"] == "white" ? Color.white : Color(red: 0.13, green: 0.14, blue: 0.14))
        let r = ImageRenderer(content: view)
        r.scale = 2
        guard let img = r.nsImage, let tiff = img.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff), let png = rep.representation(using: .png, properties: [:]) else { print("fail \(name)"); return }
        try? png.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
        print("wrote \(name).png")
    }
    shot("1_idle") { $0.phase = .idle }
    shot("1d_typing") { $0.phase = .idle; $0.typing = true }
    shot("1c_connecting") { $0.connected = false }
    shot("8_assistant_chat") { $0.phase = .thinking; $0.mode = "assistant"; $0.route = "chat"
        $0.transcript = "What's a good way to prepare for a product manager interview?"
        $0.result = "Start by practicing a few product sense questions out loud. Pick a product you use, then walk through who it's for and what you'd improve." }
    shot("9_assistant_action") { $0.phase = .acting; $0.mode = "assistant"; $0.route = "action"
        $0.transcript = "Open the downloads folder"; $0.steps = [Step(text: "Opened /Users/you/Downloads", done: true)]; $0.result = "Opened Downloads" }
    shot("2_listening_dictate") { $0.phase = .listening; $0.mode = "dictate"; $0.level = 0.8 }
    shot("3_transcribing") { $0.phase = .transcribing; $0.mode = "dictate" }
    shot("4_typed") { $0.phase = .typing; $0.mode = "dictate"; $0.transcript = "Open my resume and search for product manager jobs" }
    shot("4b_done_dictate") { $0.phase = .done; $0.mode = "dictate"; $0.transcript = "Open my resume and search for product manager jobs" }
    shot("14_job_search") {
        $0.phase = .acting; $0.mode = "prometheus"
        $0.transcript = "Can you find the best jobs for me based on my resume saved locally"
        $0.checklist = [ChecklistItem(text: "Read resume from local files", status: "done"), ChecklistItem(text: "Extract profile and key skills", status: "done"),
                        ChecklistItem(text: "Search LinkedIn Jobs for matching roles", status: "in_progress"), ChecklistItem(text: "Open top 6-8 postings", status: "pending"),
                        ChecklistItem(text: "Score each posting against resume", status: "pending"), ChecklistItem(text: "Report ranked shortlist with links", status: "pending")]
        $0.steps = [Step(text: "Opening linkedin.com/jobs/search", done: false)]
        $0.result = "Got your resume. Now searching LinkedIn for the best matching roles."
    }
    shot("10_prometheus_checklist") {
        $0.phase = .acting; $0.mode = "prometheus"
        $0.transcript = "Find a lofi playlist on YouTube and play the first one"
        $0.checklist = [ChecklistItem(text: "Open YouTube search", status: "done"), ChecklistItem(text: "Pick the first real result", status: "in_progress"),
                        ChecklistItem(text: "Press play", status: "pending")]
        $0.steps = [Step(text: "Opened youtube.com", done: true), Step(text: "Clicking: first video result", done: false)]
        $0.result = "Found it. Opening the first result."
    }
    shot("15_typing_working") {
        $0.phase = .listening; $0.mode = "prometheus"; $0.typing = true; $0.jobActive = true
        $0.transcript = "Can you open linkedin"
        $0.checklist = [ChecklistItem(text: "Opened https://www.linkedin.com", status: "done")]
        $0.result = "Opened https://www.linkedin.com"      // restates the checklist: must not appear twice
    }
    func pc(_ name: String, local: Bool = false, state: String = "idle", task: String = "", checklist: [[String: Any]] = [], step: String = "",
            result: String = "", error: String = "", detail: String = "", approval: String = "") -> [String: Any] {
        ["name": name, "local": local, "state": state, "detail": detail, "task": task, "checklist": checklist, "step": step,
         "result": result, "error": error, "approval": approval]
    }
    shot("20_swarm_working") {
        $0.phase = .thinking; $0.mode = "swarm"; $0.transcript = "Open Gmail on the MacBook and play lofi on the mini"
        $0.swarm = SwarmBoard(["picked": "auto", "routing": "named", "pcs": [
            pc("Studio", local: true),
            pc("MacBook", state: "working", task: "open gmail",
               checklist: [["text": "Open Gmail", "status": "done"], ["text": "Check the inbox", "status": "in_progress"], ["text": "Summarise unread mail", "status": "pending"]],
               step: "Reading the inbox"),
            pc("Mac mini", state: "stalled", task: "play lofi", checklist: [["text": "Search YouTube for lofi", "status": "in_progress"]], step: "Searching YouTube")]])
    }
    shot("21_swarm_done") {
        $0.phase = .idle; $0.mode = "swarm"; $0.transcript = "Check my calendar on both"
        $0.swarm = SwarmBoard(["picked": "auto", "routing": "all", "pcs": [
            pc("Studio", local: true, state: "done", task: "check my calendar", checklist: [["text": "Open Calendar", "status": "done"]], result: "Two meetings today: 10:00 standup, 15:30 review."),
            pc("MacBook", state: "done", task: "check my calendar", checklist: [["text": "Open Calendar", "status": "done"]], result: "Same two meetings."),
            pc("Mac mini", state: "offline", detail: "can't reach 192.168.1.30:8781")]])
    }
    shot("26_prometheus_listening") {                              // the widest status row: chip, glyph, status word, timer and every button
        $0.phase = .listening; $0.mode = "prometheus"; $0.listenStart = Date().addingTimeInterval(-44); $0.voiceOn = false
    }
    shot("25_swarm_long_rated") {                                  // a long quote and two finished computers: it must fit, scroll, and offer thumbs
        $0.phase = .idle; $0.mode = "swarm"
        $0.transcript = "Okay, now on Macbook, can you check my profile and see what my profile looks like and based on that, on this Mac mini, open another LinkedIn tab which can tell me the best jobs for me. Also you may want to open another tab on this Mac mini which will also tell me all posts regarding any positions for my particular profile."
        $0.swarm = SwarmBoard(["picked": "auto", "routing": "split", "pcs": [
            pc("Mac mini", local: true, state: "done", task: "Open another tab to find all posts regarding positions for your particular profile",
               checklist: [["text": "Open new LinkedIn tab", "status": "done"], ["text": "Search posts for VP AI/ML job openings", "status": "done"], ["text": "Read and surface the best post results", "status": "done"]],
               result: "The posts tab is open and already showing great results. The two top posts are VP-level AI roles in New York, both posted this week."),
            pc("MacBook", state: "done", task: "Check your profile and see what your profile looks like",
               result: "Your profile looks great. You're listed as VP, Applied AI/ML Lead in the New York City area with 1,766 followers and 500-plus connections. Your about section highlights five-plus years of experience building and shipping machine learning products.")]])
        $0.rating = "down"; $0.rateOpen = true
    }
    shot("22_swarm_pick") {
        $0.phase = .idle; $0.mode = "swarm"; $0.mapOpen = true
        $0.swarm = SwarmBoard(["picked": "MacBook", "routing": "", "pcs": [pc("Studio", local: true), pc("MacBook"), pc("Mac mini"), pc("Air")]])
    }
    shot("23_swarm_not_responding") {
        $0.phase = .thinking; $0.mode = "swarm"; $0.transcript = "Search the web on the mini"
        $0.swarm = SwarmBoard(["picked": "auto", "routing": "named", "pcs": [
            pc("Studio", local: true), pc("MacBook"),
            pc("Mac mini", state: "not_responding", task: "search the web", checklist: [["text": "Search the web", "status": "in_progress"]], detail: "no word from that computer")]])
    }
    shot("24_swarm_resting") { $0.phase = .idle; $0.isSwarmApp = true }
    shot("11_music") { $0.phase = .music; $0.mode = "prometheus"; $0.musicTitle = "Queen – Bohemian Rhapsody (Official Video Remastered)" }
    shot("13_long_speaking") { $0.phase = .thinking; $0.mode = "prometheus"; $0.speaking = true
        $0.transcript = String(repeating: "Here is a long answer that keeps going so the caption has to grow and then scroll to follow the newest words. ", count: 2) }
    shot("12_text_only") { $0.phase = .idle; $0.voiceOn = false }
    shot("5_action") { $0.phase = .acting; $0.mode = "action"; $0.transcript = "Open the downloads folder" }
    shot("6_agent_plan") {
        $0.phase = .thinking; $0.mode = "agent"; $0.session = "SESSION · TURN 2 · 4 MIN"
        $0.transcript = "Find me product manager jobs on LinkedIn that fit my resume"
        $0.plan = ["Read resume from ~/Documents", "Search LinkedIn for PM roles", "Rank by fit and summarize"]
    }
    shot("7_agent_approval") {
        $0.phase = .awaiting_approval; $0.mode = "agent"; $0.session = "SESSION · TURN 2 · 4 MIN"
        $0.transcript = "Apply to the first one"
        $0.steps = [Step(text: "Reading resume.pdf", done: true), Step(text: "Opened LinkedIn job: Senior PM, Stripe", done: true)]
        $0.approval = "Click 'Easy Apply' on Stripe — Senior PM?"
    }
}


/// `VoiceOverlay --bench`: how long one frame of each animated piece takes to build and draw, to compare their cost.
@MainActor
func runBench() {
    func pc(_ n: String, _ state: String) -> [String: Any] {
        ["name": n, "local": n == "Studio", "state": state, "detail": "", "task": "t", "checklist": [], "step": "", "result": "", "error": "", "approval": ""]
    }
    let board = SwarmBoard(["picked": "auto", "routing": "", "pcs": [pc("Studio", "idle"), pc("MacBook", "working"), pc("Mac mini", "working")]])!
    func measure(_ name: String, _ view: () -> some View) {
        var total = 0.0
        let n = 60
        for _ in 0..<n {
            let t0 = CFAbsoluteTimeGetCurrent()
            let r = ImageRenderer(content: view().frame(width: 440, height: 150))
            r.scale = 1
            _ = r.cgImage
            total += CFAbsoluteTimeGetCurrent() - t0
        }
        print(String(format: "%-28@ %6.2f ms per frame", name as NSString, total / Double(n) * 1000))
    }
    measure("ribbons (single mode)") { Ribbons(palette: .convo, level: 0.5, phase: .thinking) }
    measure("swarm map, 2 computers") { SwarmMap(board: board) { _ in } }
}
