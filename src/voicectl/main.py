from __future__ import annotations

import asyncio
import logging
import os

from voicectl.core.config import DATA_DIR, load_config
from voicectl.core.events import EventBus
from voicectl.core.maintenance import Maintenance
from voicectl.core.memory import Memory
from voicectl.core.session import SessionManager, State
from voicectl.core.wake import WakeListener
from voicectl.input.app_keys import route_app_keys
from voicectl.input.hotkeys import HotkeyListener
from voicectl.input.mic import Mic
from voicectl.telemetry import phoenix as telemetry
from voicectl.modes.conversation import PHRASES
from voicectl.tts.speaker import Speaker
from voicectl.ui_bridge.server import UIBridge
from voicectl import modes as modes_pkg
from voicectl import stt as stt_pkg
from voicectl.modes.base import ModeContext, modes
from voicectl.stt.base import backends

log = logging.getLogger("voicectl")


async def run() -> None:
    config = load_config()
    bus = EventBus()
    telemetry.setup(config)

    modes.discover(modes_pkg)
    backends.discover(stt_pkg)

    speaker = Speaker(config, bus)
    prewarm_task = asyncio.create_task(speaker.prewarm(PHRASES)) if config.get("tts.enabled", True) else None
    ctx = ModeContext(config, bus, speaker)
    mode_objs = {name: cls(ctx) for name, cls in modes.all().items()}
    stt = backends.get(config.get("stt.backend", "faster_whisper"))(config)
    mic = Mic(config.get("recording.sample_rate", 16000))

    log.info("Loading STT model…")
    await asyncio.to_thread(stt.warm_up)

    convo_stt = None
    primary = "prometheus" if "prometheus" in mode_objs else "assistant"
    if primary in mode_objs:          # conversation uses a faster model than dictation: speed matters more than the last 2% accuracy
        import copy
        c = copy.deepcopy(config.raw)
        fw = c.setdefault("stt", {}).setdefault("faster_whisper", {})
        fw["model"], fw["vad_filter"] = config.get("conversation.stt_model", "base.en"), False   # the ears already trimmed the audio
        convo_stt = backends.get(config.get("stt.backend", "faster_whisper"))(type(config)(c))
        await asyncio.to_thread(convo_stt.warm_up)
    session = SessionManager(config, bus, mic, stt, mode_objs, speaker, convo_stt)
    if primary in mode_objs and config.get("agent.warm_on_launch", True) and hasattr(mode_objs[primary], "_runner"):
        asyncio.create_task(mode_objs[primary]._runner().warm_up())      # start the agent + browser bridge now, not on your first request

    # Hourly memory hygiene: only when idle (see core/maintenance.py). Also on demand: "clean your memory", or the Dock menu.
    maint = Maintenance(config, bus, Memory(DATA_DIR / "voicectl.db"), lambda: session.state is State.IDLE,
                        lambda: ctx.shared.get("agent_runner"))
    ctx.shared["maintenance"] = maint
    if os.environ.get("VOICECTL_HOTKEYS") == "app":          # the app relaunches the daemon, so a self-restart is safe
        maint.on_restart = lambda: asyncio.get_running_loop().call_later(1.0, os._exit, 0)
    maint.start()

    async def ui_maintenance() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "maintenance":
                report = await maint.clean(force=True)
                log.info("manual memory cleanup: %s", report)
                bus.publish("notice", text=f"Memory cleaned: {report}")
    maint_task = asyncio.create_task(ui_maintenance())

    def forget_conversation() -> None:
        for m in mode_objs.values():
            if hasattr(m, "history"):
                m.history.clear()

    ctx.shared["forget_conversation"] = forget_conversation

    async def ui_fresh() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "fresh":
                log.info("start fresh: %s", await maint.start_fresh(forget_conversation))
    fresh_task = asyncio.create_task(ui_fresh())

    async def ui_newchat() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "newchat":
                log.info("%s", await maint.new_conversation(forget_conversation))
    newchat_task = asyncio.create_task(ui_newchat())

    async def ui_voice() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "voice":
                speaker.set_voice(bool(ev.data.get("on")))
    voice_task = asyncio.create_task(ui_voice())

    hotkey_map = {m: k for m in mode_objs if (k := config.get(f"hotkeys.{m}"))}
    cancel_key = config.get("hotkeys.cancel", "esc")
    app_owns_keys = os.environ.get("VOICECTL_HOTKEYS") == "app"
    key_task = None
    if app_owns_keys:       # Voice Control.app captures keys (it has the macOS permissions) and sends them over the bridge
        key_task = asyncio.create_task(route_app_keys(
            bus, {k: m for m, k in hotkey_map.items()}, cancel_key,
            session.key_down, session.key_up, session.cancel))
        hotkeys = None
    else:                   # terminal/dev: listen directly with pynput
        hotkeys = HotkeyListener(asyncio.get_running_loop(), hotkey_map, cancel_key,
                                 session.key_down, session.key_up, session.cancel)
        hotkeys.start()

    bridge = UIBridge(bus, config.get("ui.ws_host", "127.0.0.1"), config.get("ui.ws_port", 8765),
                      hello={"hotkeys": hotkey_map, "cancel": cancel_key},
                      snapshot=lambda: [{"type": "voice", "data": {"on": speaker.voice_on}}])
    bridge_task = asyncio.create_task(bridge.run())

    async def log_events() -> None:
        async for ev in bus.subscribe():
            if ev.type == "level":
                continue
            if ev.type == "transcript":
                log.info("📝 [%s] %s", ev.data["mode"], ev.data["text"])
            elif ev.type == "notice":
                log.info("⚠️  %s", ev.data["text"])
            elif ev.type == "error":
                log.error("❌ %s", ev.data["message"])
            else:
                log.info("%s %s", ev.type, ev.data)

    wake = None
    if primary in mode_objs and config.get("wake_word.enabled", True):
        import copy
        wake_cfg = copy.deepcopy(config.raw)
        wake_cfg.setdefault("stt", {}).setdefault("faster_whisper", {})["model"] = config.get("wake_word.model", "base.en")
        wake_cfg["stt"]["backend"] = "faster_whisper"          # always local: this listens to the room all day, nothing may leave the Mac
        wake_stt = backends.get("faster_whisper")(type(config)(wake_cfg))
        await asyncio.to_thread(wake_stt.warm_up)
        wake = WakeListener(config, bus, Mic(config.get("recording.sample_rate", 16000)), wake_stt,
                            lambda: session.state is State.IDLE and not speaker.active,
                            lambda rest, heard: session.wake(primary, rest, heard))
        wake.start()

    async def ui_text() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "text" and primary in mode_objs:
                typed = str(ev.data.get("text", "")).strip()
                if not typed:
                    continue
                if session.conversation_active():
                    await mode_objs[primary].submit_text(typed)          # interrupts what it is saying or doing
                else:
                    session.wake(primary, typed)                          # not in a conversation yet: start one with your message
    text_task = asyncio.create_task(ui_text())

    async def ui_input_mode() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "input_mode" and primary in mode_objs:
                mode = mode_objs[primary]
                if hasattr(mode, "set_typed_only"):
                    mode.set_typed_only(ev.data.get("mode") == "typed")
    input_mode_task = asyncio.create_task(ui_input_mode())

    async def ui_wake_toggle() -> None:
        async for ev in bus.subscribe():
            if ev.type == "ui_command" and ev.data.get("command") == "wake" and wake:
                wake.set_enabled(bool(ev.data.get("enabled")))
    wake_toggle_task = asyncio.create_task(ui_wake_toggle())

    keys = ", ".join(f"{k.upper()}={m}" for m, k in hotkey_map.items())
    log.info("Ready. %s (%s mode). Ctrl+C to quit.", keys, config.get("recording.mode", "toggle"))
    try:
        await log_events()
    finally:
        if hotkeys:
            hotkeys.stop()
        if key_task:
            key_task.cancel()
        bridge_task.cancel()
        wake_toggle_task.cancel()
        maint_task.cancel()
        voice_task.cancel()
        maint.stop()
        if wake:
            wake.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
