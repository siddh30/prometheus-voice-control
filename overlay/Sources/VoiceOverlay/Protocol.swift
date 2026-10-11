import Foundation

/// Everything the widget can say to the daemon, in one place. The names are the daemon's command names
/// (voicectl/api/protocol.py); a Python test fails if the two lists ever differ.
enum CommandName: String, CaseIterable {
    case text, input_mode, voice, wake, newchat, fresh, maintenance, key, approval, input_result
}

enum Command {
    case text(String)
    case inputMode(typed: Bool)
    case voice(on: Bool)
    case wake(enabled: Bool)
    case newChat
    case fresh
    case maintenance
    case key(name: String, down: Bool)
    case approval(Bool)
    case inputResult(id: String, fields: [String: Any])

    var payload: [String: Any] {
        switch self {
        case .text(let t): return ["type": CommandName.text.rawValue, "text": t]
        case .inputMode(let typed): return ["type": CommandName.input_mode.rawValue, "mode": typed ? "typed" : "voice"]
        case .voice(let on): return ["type": CommandName.voice.rawValue, "on": on]
        case .wake(let enabled): return ["type": CommandName.wake.rawValue, "enabled": enabled]
        case .newChat: return ["type": CommandName.newchat.rawValue]
        case .fresh: return ["type": CommandName.fresh.rawValue]
        case .maintenance: return ["type": CommandName.maintenance.rawValue]
        case .key(let name, let down): return ["type": CommandName.key.rawValue, "name": name, "down": down]
        case .approval(let ok): return ["type": CommandName.approval.rawValue, "approved": ok]
        case .inputResult(let id, let fields):
            return fields.merging(["type": CommandName.input_result.rawValue, "id": id]) { _, new in new }
        }
    }

    /// The JSON text sent over the socket; nil only if the payload could not be encoded.
    var json: String? {
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
