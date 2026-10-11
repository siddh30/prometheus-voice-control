import Foundation
import SwiftUI

/// How much the widget may animate. Every moving thing asks here: in Calm mode nothing redraws on a timer, which leaves the desktop's drawing
/// to everything else (useful on a busy or high-refresh multi-monitor setup).
enum UIMotion {
    nonisolated(unsafe) static var calm: Bool = UserDefaults.standard.bool(forKey: "calm")
    /// The display's own refresh rate (as smooth as it gets), and nothing at all in Calm mode.
    static var full: AnimationTimelineSchedule { AnimationTimelineSchedule(minimumInterval: nil, paused: calm) }
    /// A timeline that ticks at most `fps` times a second, and not at all in Calm mode.
    static func schedule(_ fps: Double) -> AnimationTimelineSchedule { AnimationTimelineSchedule(minimumInterval: 1.0 / fps, paused: calm) }
}
