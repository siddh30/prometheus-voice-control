import pytest

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes.base import ModeContext
from voicectl.modes.tv_control import TvControlMode
from voicectl.tv.client import TvUnavailable
from voicectl.tv.commands import TvCommand, parse


def key(text, **kw):
    cmd = parse(text, **kw)
    return (cmd.kind, cmd.key, cmd.repeat) if cmd else None


def test_volume_mute_and_navigation():
    assert key("volume up") == ("key", "VOLUME_UP", 2)
    assert key("Turn the volume down by five.") == ("key", "VOLUME_DOWN", 5)
    assert key("turn it up 3") == ("key", "VOLUME_UP", 3)
    assert key("louder") == ("key", "VOLUME_UP", 2)
    assert key("mute") == ("key", "MUTE", 1)
    assert key("go home") == ("key", "HOME", 1)
    assert key("go back please") == ("key", "BACK", 1)
    assert key("move left") == ("key", "DPAD_LEFT", 1)
    assert key("pause") == ("key", "MEDIA_PLAY_PAUSE", 1)


def test_volume_steps_are_capped():
    assert key("volume up 50", max_steps=10) == ("key", "VOLUME_UP", 10)
    assert key("volume up 50", max_steps=4) == ("key", "VOLUME_UP", 4)
    assert key("volume up 0") == ("key", "VOLUME_UP", 1)


def test_power():
    off = parse("turn off the TV")
    assert (off.kind, off.want) == ("power", "off")
    on = parse("TV on")
    assert (on.kind, on.want) == ("power", "on")


def test_apps_by_name_and_extra_apps():
    nf = parse("open Netflix on the TV")
    assert (nf.kind, nf.package) == ("launch", "com.netflix.ninja")
    assert parse("play youtube").package == "com.google.android.youtube.tv"
    assert parse("Disney plus").package == "com.disney.disneyplus"
    assert parse("open crunchyroll") is None
    assert parse("open crunchyroll", apps={"Crunchyroll": "com.crunchyroll.crunchyroid"}).package == "com.crunchyroll.crunchyroid"


def test_inputs_and_aliases():
    assert key("switch to hdmi 2") == ("key", "TV_INPUT_HDMI_2", 1)
    assert parse("switch to hdmi 7") is None
    assert parse("switch to the playstation") is None
    assert key("switch to the playstation", inputs={"playstation": 3}) == ("key", "TV_INPUT_HDMI_3", 1)
    assert key("show inputs") == ("key", "TV_INPUT", 1)


@pytest.mark.parametrize("text", [
    "", "what is the weather", "install an app", "open settings and factory reset", "disable the launcher",
    "run a shell command", "delete everything", "type my password", "open netflix and then delete it",
    "volume up and open the web browser",
])
def test_anything_else_is_refused(text):
    assert parse(text) is None


class FakeClient:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    async def run(self, cmd: TvCommand) -> str:
        if self.error:
            raise self.error
        self.sent.append(cmd)
        return cmd.label


def make_mode(client, **tv):
    bus = EventBus()
    seen = []
    real = bus.publish
    bus.publish = lambda type, **data: (seen.append((type, data)), real(type, **data))[1]
    spoken = []

    class Speaker:
        def enabled_for(self, mode):
            return True

        async def speak(self, text):
            spoken.append(text)

    tv.setdefault("ai", False)                              # no test may reach a real model unless it installs a fake one
    ctx = ModeContext(Config({"tv": tv}), bus, Speaker())
    mode = TvControlMode(ctx)
    mode.client = client
    return mode, seen, spoken


async def test_mode_sends_the_command_and_says_it():
    client = FakeClient()
    mode, seen, spoken = make_mode(client)
    await mode.handle("volume up three")
    assert [(c.key, c.repeat) for c in client.sent] == [("VOLUME_UP", 3)]
    assert ("step", {"text": "Volume up", "done": True}) in seen
    assert spoken == ["Volume up"]


async def test_mode_refuses_what_is_not_on_the_list():
    client = FakeClient()
    mode, seen, spoken = make_mode(client)
    await mode.handle("factory reset the television")
    assert client.sent == []
    assert spoken == ["I can't do that on the TV."]
    assert any(t == "notice" for t, _ in seen)


async def test_mode_says_when_the_tv_cannot_be_reached():
    mode, seen, spoken = make_mode(FakeClient(TvUnavailable("I can't reach the TV. It may be off or on another network.")))
    await mode.handle("mute")
    assert spoken == ["I can't reach the TV. It may be off or on another network."]
    assert ("step", {"text": spoken[0], "done": False, "failed": True}) in seen


async def test_mode_uses_the_configured_volume_cap_and_inputs():
    client = FakeClient()
    mode, _, _ = make_mode(client, max_volume_steps=3, inputs={"playstation": 2})
    await mode.handle("volume up 9")
    await mode.handle("switch to the playstation")
    assert [(c.key, c.repeat) for c in client.sent] == [("VOLUME_UP", 3), ("TV_INPUT_HDMI_2", 1)]


# ---- chains, exact volume, and the model's plan form ----

from voicectl import llm
from voicectl.llm import ToolCall
from voicectl.tv.commands import DEFAULT_APPS, parse_many
from voicectl.tv.planner import validate


def kinds(cmds):
    return [c.kind for c in cmds] if cmds else cmds


def test_exact_volume_and_chains():
    c = parse("set volume to 30")
    assert (c.kind, c.repeat) == ("volume_to", 30)
    assert parse("volume 250").repeat == 100
    assert kinds(parse_many("open youtube and turn the volume down")) == ["launch", "wait", "key"]   # waits for the app to come up
    assert kinds(parse_many("mute, then go home")) == ["key", "key"]
    assert kinds(parse_many("open netflix")) == ["launch"]
    assert parse_many("open netflix and format the disk") is None                  # one unknown part refuses the whole sentence
    assert parse_many("mute and mute and mute and mute and mute and mute") is None  # no longer than five parts


GOOD = {"summary": "Opening Netflix and pressing play.",
        "steps": [{"op": "open_app", "app": "netflix"}, {"op": "wait", "seconds": 8}, {"op": "key", "key": "MEDIA_PLAY_PAUSE"}]}
GOOD_NO_TOGGLE = {"summary": "Opening Netflix, which resumes by itself.", "steps": [{"op": "open_app", "app": "netflix"}]}


def test_a_good_plan_becomes_commands():
    cmds, summary = validate(GOOD_NO_TOGGLE, DEFAULT_APPS)
    assert kinds(cmds) == ["launch"] and cmds[0].package == "com.netflix.ninja"
    assert summary == "Opening Netflix, which resumes by itself."


def test_a_play_pause_press_right_after_opening_an_app_is_dropped():
    """Netflix resumes by itself when opened; a toggle after it would pause it."""
    cmds, _ = validate(GOOD, DEFAULT_APPS)
    assert kinds(cmds) == ["launch"]                                     # the wait that spaced it out goes too
    cmds, _ = validate({"steps": [{"op": "key", "key": "MEDIA_PLAY_PAUSE"}]}, DEFAULT_APPS)
    assert kinds(cmds) == ["key"]                                        # on its own (pause or play what is on) it stays
    cmds, _ = validate({"steps": [{"op": "open_app", "app": "netflix"}, {"op": "wait", "seconds": 5}, {"op": "key", "key": "VOLUME_DOWN"}]}, DEFAULT_APPS)
    assert kinds(cmds) == ["launch", "wait", "key"]                      # other keys after a launch are kept


@pytest.mark.parametrize("bad", [
    {"steps": []},
    {"steps": [{"op": "shell", "cmd": "rm -rf /"}]},
    {"steps": [{"op": "key", "key": "POWER"}]},                                   # not on the key list
    {"steps": [{"op": "key", "key": "VOLUME_UP", "repeat": 11}]},
    {"steps": [{"op": "open_app", "app": "crunchyroll"}]},                         # not a known app
    {"steps": [{"op": "volume_to", "level": 101}]},
    {"steps": [{"op": "volume_to", "level": True}]},
    {"steps": [{"op": "wait", "seconds": 16}]},
    {"steps": [{"op": "wait", "seconds": 14}, {"op": "wait", "seconds": 14}]},     # more than 25 s of waiting in all
    {"steps": [{"op": "hdmi", "port": 5}]},
    {"steps": [{"op": "youtube_search", "query": "x" * 81}]},
    {"steps": [{"op": "youtube_search", "query": "   "}]},
    {"steps": [{"op": "key", "key": "HOME"}] * 7},                                 # longer than the plan limit
    {"steps": [{"op": "key", "key": "HOME"}, "install an app"]},                   # one bad step refuses the whole plan
    {"steps": "open netflix"},
    "open netflix",
    None,
])
def test_a_plan_with_anything_off_is_refused_whole(bad):
    assert validate(bad, DEFAULT_APPS) is None


def test_youtube_search_becomes_an_encoded_link():
    cmds, _ = validate({"steps": [{"op": "youtube_search", "query": "lofi & chill\n beats"}]}, DEFAULT_APPS)
    assert (cmds[0].kind, cmds[0].package) == ("link", "https://www.youtube.com/results?search_query=lofi+%26+chill+beats")


class FakeLLM:
    def __init__(self, args=None, error=None):
        self.args, self.error, self.calls = args, error, []

    def tool_calls(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return [ToolCall("tv_plan", self.args)] if self.args is not None else []


async def test_a_sentence_the_parser_does_not_know_goes_through_the_model():
    fake = FakeLLM(GOOD_NO_TOGGLE)
    llm.set_llm(fake)
    client = FakeClient()
    mode, seen, spoken = make_mode(client, ai=True)
    await mode.handle("Open the recent show that I was watching on Netflix and start playing.")
    assert kinds(client.sent) == ["launch"]
    assert spoken == ["Opening Netflix, which resumes by itself."]
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "tv_plan"}                    # it can only fill in the form
    schema = call["tools"][0]["input_schema"]["properties"]["steps"]["items"]["properties"]
    assert "netflix" in schema["app"]["enum"] and "POWER" not in schema["key"]["enum"]


async def test_the_model_is_not_asked_when_the_parser_knows_the_sentence_or_ai_is_off():
    fake = FakeLLM(GOOD)
    llm.set_llm(fake)
    mode, _, _ = make_mode(FakeClient(), ai=True)
    await mode.handle("open netflix and turn the volume down")
    assert fake.calls == []
    mode, _, spoken = make_mode(FakeClient(), ai=False)
    await mode.handle("put something nice on")
    assert fake.calls == [] and spoken == ["I can't do that on the TV."]


@pytest.mark.parametrize("llm_args, llm_error", [
    ({"steps": [{"op": "key", "key": "POWER"}]}, None),                                  # an invalid plan
    (None, None),                                                                         # the model chose no plan
    (None, RuntimeError("no network")),                                                   # the model could not be reached
])
async def test_a_bad_or_missing_plan_sends_nothing_and_says_so(llm_args, llm_error):
    llm.set_llm(FakeLLM(llm_args, llm_error))
    client = FakeClient()
    mode, _, spoken = make_mode(client, ai=True)
    await mode.handle("do something clever")
    assert client.sent == [] and spoken == ["I can't do that on the TV."]
