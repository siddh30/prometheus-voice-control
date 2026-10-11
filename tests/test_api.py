import asyncio

from voicectl.api import COMMANDS, Control, parse_command
from voicectl.core.events import EventBus


def test_every_command_is_validated_before_anything_runs():
    assert parse_command({"command": "voice", "on": True}).args == {"on": True}
    assert parse_command({"type": "text", "text": "hi"}).name == "text"           # the widget's own shape works too
    assert parse_command({"command": "newchat"}).name == "newchat"
    assert parse_command({"command": "voice"}) is None                            # missing field
    assert parse_command({"command": "voice", "on": "yes"}) is None               # wrong type
    assert parse_command({"command": "input_mode", "mode": "telepathy"}) is None  # not a known mode
    assert parse_command({"command": "launch_missiles"}) is None                  # not a command
    assert parse_command({}) is None
    assert parse_command({"command": "input_result", "id": "1", "image": "xx"}).get("image") == "xx"   # extra payload is kept


def test_the_protocol_names_match_what_the_widget_sends():
    import pathlib, re
    swift = "".join(p.read_text() for p in pathlib.Path("overlay/Sources/VoiceOverlay").glob("*.swift"))
    sent = set(re.findall(r'\\"type\\":\\"([a-z_]+)\\"', swift)) | set(re.findall(r'"type": "([a-z_]+)"', swift))
    assert sent and sent <= set(COMMANDS), f"the widget sends commands the daemon does not define: {sent - set(COMMANDS)}"


async def test_one_dispatcher_routes_commands_and_a_bad_handler_hurts_nobody():
    bus, seen = EventBus(), []
    control = Control(bus)
    control.on("voice", lambda c: seen.append(("voice", c["on"])))
    control.on("voice", lambda c: 1 / 0)                                          # fails: logged, others still run
    control.on("voice", lambda c: seen.append("after"))

    @control.on("text")
    async def slow(c):
        await asyncio.sleep(0.2)
        seen.append(("text", c["text"]))

    task = asyncio.create_task(control.run())
    await asyncio.sleep(0.01)
    bus.publish("ui_command", command="text", text="slow one")
    bus.publish("ui_command", command="voice", on=False)
    bus.publish("ui_command", command="bogus")
    await asyncio.sleep(0.05)
    assert seen == [("voice", False), "after"]                                    # a slow command never holds up another
    await asyncio.sleep(0.3)
    assert ("text", "slow one") in seen
    task.cancel()


async def test_the_same_command_keeps_its_order():
    bus, seen = EventBus(), []
    control = Control(bus)

    @control.on("text")
    async def h(c):
        await asyncio.sleep(0.05 if c["text"] == "first" else 0)
        seen.append(c["text"])

    task = asyncio.create_task(control.run())
    await asyncio.sleep(0.01)
    bus.publish("ui_command", command="text", text="first")
    bus.publish("ui_command", command="text", text="second")
    await asyncio.sleep(0.3)
    assert seen == ["first", "second"]
    task.cancel()


def test_every_event_the_daemon_publishes_is_in_the_contract():
    import pathlib, re
    from voicectl.api import EVENTS
    found = set()
    for p in pathlib.Path("src/voicectl").rglob("*.py"):
        found |= set(re.findall(r'publish\(\s*["\']([a-z_]+)["\']', p.read_text()))
    found.discard("ui_command")                                                   # the widget -> daemon direction, see COMMANDS
    assert found and found <= set(EVENTS), f"published but not in api/events.py: {found - set(EVENTS)}"


def test_every_event_the_widget_understands_is_in_the_contract():
    import pathlib, re
    from voicectl.api import EVENTS
    model = pathlib.Path("overlay/Sources/VoiceOverlay/Model.swift").read_text()
    body = model[model.index("func handle(type:"):model.index("private var lastPhaseChange")]
    handled = set(re.findall(r'case "([a-z_]+)"', body))
    assert handled and handled <= set(EVENTS), f"the widget handles events the daemon never defines: {handled - set(EVENTS)}"


def test_event_payloads_are_checked_against_the_contract():
    from voicectl.api import validate_event
    assert validate_event("state", {"state": "idle", "mode": None}) is None
    assert validate_event("step", {"text": "Opened Gmail", "done": True}) is None
    assert validate_event("step", {"done": True}) == "step: missing 'text'"
    assert "wrong type" in validate_event("voice", {"on": "yes"})
    assert "unknown event type" in validate_event("sparkles", {})
