# Architecture: the main app and every API pathway

The app is a **widget** (Swift) and a **daemon** (Python) that only talk through two small, typed contracts. Inside the
daemon, the two ways a request arrives (voice, typing) are separate channels that feed one core. Every diagram below
is one pathway; the code that owns it is named under it.

## 1. The main app

```mermaid
flowchart LR
    subgraph APP["Voice Control.app (Swift)"]
        HK["HotkeyTap<br/>F9 / F10 / Esc"]
        UI["Widget<br/>(pill, checklist, type box)"]
        SC["ScreenControl<br/>screenshot, mouse, keys"]
    end

    subgraph DAEMON["Daemon (Python)"]
        CTRL["api.Control<br/>validates + routes commands"]
        VOICE["channels.VoiceChannel<br/>mic, speech detection, echo"]
        TEXT["channels.TextChannel<br/>typing, typing mode"]
        CORE["modes: Conversation / Prometheus<br/>turns, routing, history"]
        AGENT["agents.runner<br/>Claude agent + tools"]
        MEM[("memory + learnings<br/>SQLite")]
        TTS["tts.Speaker"]
        BUS(("EventBus"))
    end

    HK -- "key" --> CTRL
    UI -- "text, input_mode, voice, newchat ..." --> CTRL
    CTRL --> TEXT
    CTRL --> CORE
    VOICE -- "utterance text" --> CORE
    TEXT -- "typed text" --> CORE
    CORE <--> AGENT
    CORE <--> MEM
    CORE --> TTS
    CORE --> BUS
    AGENT --> BUS
    BUS -- "events (state, step, result ...)" --> UI
    AGENT -- "input_request" --> SC
    SC -- "input_result" --> CTRL
```

Owns it: `src/voicectl/main.py` wires everything; `overlay/Sources/VoiceOverlay/` is the app.

## 2. The voice path

```mermaid
flowchart LR
    MIC["Microphone<br/>(always-open stream)"] --> EARS["Ears<br/>cuts audio into utterances"]
    EARS --> VAD["VadEndpointer<br/>when did you stop?"]
    VAD --> STT["STT backend<br/>whisper-1 / local faster-whisper"]
    STT --> ECHO{"is_echo?<br/>our own voice?"}
    ECHO -- yes --> DROP["ignored,<br/>duplex steps down after 2"]
    ECHO -- no --> TURN["UserTurn text<br/>to the core"]
    DUP["decide_duplex<br/>headphones: full<br/>speakers: echo / half"] -.-> EARS
    HEALTH["mic health<br/>silent / changed / gone"] -.-> NOTICE["notice event<br/>(not while typing)"]
    EARS -. "speech started" .-> CUT["cut off the current reply"]
```

Owns it: `channels/voice.py`, `core/ears.py`, `core/vad.py`, `core/echo.py`, `stt/`.

## 3. The typing path

```mermaid
flowchart LR
    BOX["Type box<br/>(widget)"] -- "input_mode: typed" --> CTRL["api.Control"]
    CTRL --> EXCL["TextChannel.exclusive = true"]
    EXCL --> PAUSE["VoiceChannel.pause(true)<br/>mic never opened, voice ignored"]
    BOX -- "text" --> CTRL
    CTRL --> SUBMIT["submit_text<br/>cut off, becomes newest instruction"]
    SUBMIT --> CORE["core turn"]
    BOX -- "closed: input_mode voice" --> CTRL
    CTRL --> RESUME["VoiceChannel.pause(false)<br/>voice is back"]
```

While the box is open the widget hides the waveform, the listening timer, the voice toggle and the hints
(`OverlayModel.usesMic`). Owns it: `channels/text.py`, `main.py` (`input_mode`, `text` handlers).

## 4. The command path (widget to daemon)

```mermaid
flowchart LR
    W["Widget<br/>builds a JSON message"] --> WS["WebSocket<br/>ui_bridge.UIBridge"]
    WS --> PUB["bus: ui_command"]
    PUB --> PARSE{"parse_command<br/>known name, right fields?"}
    PARSE -- no --> LOG["logged and ignored"]
    PARSE -- yes --> DISPATCH["Control.dispatch<br/>one lock per command name"]
    DISPATCH --> H1["handler"]
    DISPATCH --> H2["handler"]
    H1 -. "an exception is logged,<br/>never stops the others" .-> DISPATCH
```

Commands: `text`, `input_mode`, `voice`, `wake`, `newchat`, `fresh`, `maintenance`, `key`, `approval`,
`input_result`. The table is `api/protocol.py`; a slow command never holds up a different one, and the same command
keeps its order. Owns it: `api/`, `ui_bridge/server.py`.

## 5. The event path (daemon to widget)

```mermaid
flowchart LR
    PUBLISHERS["modes, agents, speaker, wake, maintenance<br/>bus.publish(type, ...)"] --> BUS(("EventBus<br/>slow subscriber drops oldest"))
    BUS --> BRIDGE["UIBridge<br/>JSON over WebSocket"]
    HB["heartbeat every 3 s"] --> BRIDGE
    BRIDGE --> INGEST["widget Ingest<br/>off the main thread<br/>mic level collapsed to latest"]
    INGEST -- "30 Hz tick" --> MODEL["OverlayModel.handle<br/>one batch of updates"]
    MODEL --> VIEW["SwiftUI view"]
    ANIM["animations<br/>own clock (TimelineView)"] --> VIEW
    INGEST -. "silent for 8 s" .-> STALL["pill: NOT RESPONDING"]
```

The widget's animation never waits on the daemon: the daemon only changes what is shown. The event table is
`api/events.py`; the test suite checks every event ever published against it. Owns it: `api/events.py`,
`ui_bridge/server.py`, `Socket.swift`, `Model.swift`.

## 6. How a request is answered

```mermaid
flowchart TB
    IN["text from voice or typing"] --> GOODBYE{"goodbye?"}
    GOODBYE -- yes --> END["farewell, back to idle"]
    GOODBYE -- no --> INTERCEPT{"spoken command?<br/>stop, text only, new chat"}
    INTERCEPT -- yes --> DONE1["handled, no model"]
    INTERCEPT -- no --> INSTANT{"instant pattern?<br/>scroll, back, new tab"}
    INSTANT -- yes --> BROWSER["run on the browser,<br/>about 0.3 s"]
    INSTANT -- no --> ROUTE{"router<br/>chat or task?"}
    ROUTE -- chat --> CHAT["quick chat reply<br/>streamed, then spoken"]
    ROUTE -- task --> AGENT["Claude agent<br/>checklist, steps, tools"]
    AGENT --> CHECKLIST["checklist events<br/>(progress, not an answer)"]
    AGENT --> RESULT["result event<br/>only if it adds new information"]
    CHAT --> SPEAK["Speaker (TTS)"]
    RESULT --> SPEAK
```

Owns it: `modes/prometheus.py`, `modes/instant.py`, `modes/router.py`, `agents/runner.py`.

## 7. The screen and mouse path

```mermaid
sequenceDiagram
    participant A as Agent (daemon)
    participant B as UIBridge
    participant S as Widget socket
    participant Q as Screen queue (background)
    A->>B: event input_request {id, op}
    B->>S: JSON
    S->>Q: run off the UI thread
    Q->>Q: screencapture / mouse / keys
    Q->>B: command input_result {id, ...}
    B->>A: resolves the waiting call
```

Screenshots, clicks and key presses never run on the widget's main thread. Owns it: `agents/screen.py`,
`ScreenControl.swift`, `Socket.swift`.

## 8. The memory path

```mermaid
flowchart LR
    TURN["each turn"] --> TURNS[("turns<br/>last 24 h")]
    TURNS --> RECAP["recap on restart<br/>last turns"]
    TURNS --> COMPACT["after 20 turns:<br/>summarise, strip tool calls"]
    COMPACT --> SESSIONS[("sessions<br/>short notes")]
    TURN --> FACTS[("facts<br/>what it knows about you")]
    TASK["finished task"] --> LEARN[("learnings.db<br/>how to do things")]
    LEARN -- "matching learning<br/>applied to the next similar task" --> AGENT["agent prompt"]
    FACTS --> AGENT
    SESSIONS --> AGENT
    MAINT["hourly maintenance,<br/>only when idle"] --> TURNS
    MAINT --> SESSIONS
```

Owns it: `core/memory.py`, `core/learnings.py`, `core/maintenance.py`.

## Rules the code follows

- The widget and the daemon share only `api/protocol.py` (commands) and `api/events.py` (events). Tests fail if
  either side uses something that isn't in them.
- Voice and typing are separate channels. Nothing in the core asks which one a request came from.
- The widget's animation runs on its own clock. Backend messages are buffered off the main thread and applied in
  batches; if the daemon goes quiet the widget says so and keeps animating.
- Anything slow (screenshots, clicks, network) runs off the UI thread.
