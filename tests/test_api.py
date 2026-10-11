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


def test_the_widget_and_the_daemon_define_exactly_the_same_commands():
    import pathlib, re
    swift = pathlib.Path("overlay/Sources/VoiceOverlay/Protocol.swift").read_text()
    enum = swift[swift.index("enum CommandName"):swift.index("enum Command {")]
    names = set(re.findall(r"[a-z_]+", enum[enum.index("case "):].replace("case ", " ")))
    names -= {"enum", "string", "caseiterable"}
    assert names == set(COMMANDS), f"widget only: {names - set(COMMANDS)}; daemon only: {set(COMMANDS) - names}"


def test_no_part_of_the_widget_builds_command_json_by_hand():
    import pathlib, re
    for p in pathlib.Path("overlay/Sources/VoiceOverlay").glob("*.swift"):
        if p.name == "Protocol.swift":
            continue
        assert not re.search(r'\\"type\\":\\"(text|input_mode|voice|wake|newchat|fresh|maintenance|key|approval|input_result)\\"', p.read_text()), \
            f"{p.name} hand-builds a command: use Command in Protocol.swift"


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


async def test_a_request_gets_the_answer_with_its_own_id_and_times_out_when_nobody_answers():
    import pytest
    from voicectl.api import RequestBroker, RequestTimeout
    bus = EventBus()
    broker = RequestBroker(bus)
    control = Control(bus)
    control.on("input_result", broker.resolve)
    control.start()

    async def app():                                                              # a stand-in for the widget
        async for ev in bus.subscribe():
            if ev.type == "input_request":
                bus.publish("ui_command", command="input_result", id="wrong", ok=False)   # someone else's answer: ignored
                bus.publish("ui_command", command="input_result", id=ev.data["id"], ok=True, image="abc")
                return
    answerer = asyncio.create_task(app())
    await asyncio.sleep(0.01)
    res = await broker.request("input_request", 1.0, op="screenshot")
    assert res["ok"] is True and res["image"] == "abc"
    await answerer
    with pytest.raises(RequestTimeout):
        await broker.request("input_request", 0.05, op="screenshot")
    control.stop()


async def test_the_screen_link_turns_a_failed_or_missing_answer_into_one_clear_error():
    import pytest
    from voicectl.agents.screen import ScreenError, ScreenLink

    class Broker:
        def __init__(self, result): self.result = result
        async def request(self, *a, **k):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    from voicectl.api import RequestTimeout
    assert (await ScreenLink(Broker({"ok": True, "x": 1})).request("screenshot"))["x"] == 1
    with pytest.raises(ScreenError, match="Screen recording"):
        await ScreenLink(Broker({"ok": False, "error": "Screen recording is off"})).request("screenshot")
    with pytest.raises(ScreenError, match="didn't answer"):
        await ScreenLink(Broker(RequestTimeout("input_request"))).request("screenshot")


async def test_an_approval_from_the_widget_reaches_the_waiting_agent_through_the_dispatcher():
    from voicectl.agents.runner import AgentRunner
    runner = AgentRunner.__new__(AgentRunner)
    runner._approval = asyncio.get_running_loop().create_future()
    bus = EventBus()
    control = Control(bus)
    control.on("approval", lambda cmd: runner.respond_approval(cmd["approved"]))
    control.start()
    await asyncio.sleep(0.01)
    bus.publish("ui_command", command="approval", approved=True)
    assert await asyncio.wait_for(runner._approval, 1.0) is True
    control.stop()
