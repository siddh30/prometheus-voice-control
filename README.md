# voicectl: Prometheus

Voice control for macOS. Say **"Prometheus"** (or press F10) and talk; it talks back and does things for you.

| Key | Mode | What it does |
|-----|------|--------------|
| F9  | **Dictate** | Press, talk, press again: your words are typed at the cursor of any app (`small.en`, most accurate). |
| F10 | **Prometheus** | The main agent. One Claude brain that converses AND acts: your own logged-in Chrome (search YouTube and play a song, click, type, scroll, go back), the screen (it looks and uses the mouse), your files, the web, quick actions (open apps, volume, sleep). It narrates what it is doing, shows a checklist, and you can talk over it or press F10 to interrupt. Esc or "goodbye" ends. |

## Setup (one time)
1. `scripts/build_app.sh` installs `~/Applications/Voice Control.app`; open it and keep it in the Dock.
2. System Settings > Privacy & Security, switch Voice Control on under: **Accessibility**, **Input Monitoring**, **Microphone**, **Screen Recording** (only needed for the screen-looking tools).
3. Browser control uses your own Chrome through Google's official `chrome-devtools-mcp`:
   `brew install node && npm install -g chrome-devtools-mcp`, then in Chrome open `chrome://inspect/#remote-debugging` and switch on **Remote Debugging** (click **Allow** if Chrome asks).
   Prometheus uses ONE tab of your Chrome (no new browser, no new window); your logins are already there. Without Node it falls back to Chrome's AppleScript control (`View > Developer > Allow JavaScript from Apple Events`).

## Two versions: yours and mine (so testing never mixes)
- **Stable** = the `Voice Control` app. It runs from a frozen snapshot (`~/Library/Application Support/VoiceControl/releases/<time>`), with your memory in `.../VoiceControl/data`. Edits and test runs in this folder cannot change it.
- **Dev** = this working folder. Own data (`data-dev/`), port 8766, no hotkeys, no mic or wake word, silent, hidden test browser, never your Chrome. Run it with `scripts/dev.sh test|run|stop`.
- **Release** only when you want an update: `scripts/release.sh "what changed"`, then quit and reopen the app. Roll back: `scripts/build_app.sh "<older release folder>"`.

## Voice or text only
The small speaker button on the widget (also the Dock menu, or saying "text only" / "speak to me") switches between spoken replies and text only. It is remembered between launches. In text-only mode nothing is played, so on speakers there is nothing for the mic to hear.

## Music
Ask for audio ("play X on YouTube") and, once it plays, the music keeps going and Prometheus keeps listening but only acts on what starts with **"Prometheus"** (or on what you say right after pressing F10). Anything else, like lyrics, is ignored. It returns to normal when the audio stops.

## Memory
Every hour, when idle, Prometheus tidies its memory: a session older than an hour is summarised into a short note and closed (so the next request starts light but remembers what mattered); old logs and caches are pruned; and if the background process ever grows past `memory.max_rss_mb` it restarts itself. Say **"clean your memory"** or use the Dock menu for it on demand. Facts you asked it to remember (resume summary etc.) are never auto-deleted. Settings: `memory:` in `config.yaml`.

## Extend
- New quick action: `src/voicectl/actions/<name>.py` with an `@actions.register(...)` class.
- New agent skill: `.claude/skills/<name>/SKILL.md`. Its voice and manners: `prompts/prometheus_prompt.txt`.
- New mode / hotkey: `src/voicectl/modes/<name>.py` and a key under `hotkeys:` in `config.yaml`.

## Under the hood
Claude Agent SDK (Sonnet) with browser / screen / checklist / memory tools · Edge TTS voices (cached, word-synced captions) ·
faster-whisper (`base.en` for conversation, `small.en` for dictation) · Silero VAD ears (full duplex: it hears you while it speaks) ·
TypeSafe Jev (instant-command detection, risk scoring for approvals) · Phoenix traces at http://localhost:6006.
