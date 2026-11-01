import pytest

from voicectl.core.config import Config
from voicectl.core.events import EventBus
from voicectl.modes.base import ModeContext
from voicectl.modes.tv_control import TvControlMode
from voicectl.tv.commands import DEFAULT_APPS, parse, parse_many
from voicectl.platform.displays import DisplayConfigError
from voicectl.tv.display import DisplayClient, DisplayError, extend_script, move_script, parse_screens, stop_script
from voicectl.tv.planner import validate

ID = "C0:92:1C:67:48:2E"
MAC = "Mac mini|0|0|2560|1440"
TV = "SmartTV 4K FFM|2560|0|1920|1080"


class FakeMac:
    """A stand-in for macOS: which screens exist, what the scripts did, and whether the extend click 'worked'."""

    def __init__(self, tv_present=False, extend_works=True, running=("Citrix Viewer",)):
        self.tv, self.extend_works, self.running = tv_present, extend_works, set(running)
        self.scripts, self.moved, self.moves = [], [], []

    async def __call__(self, script, language=None, timeout=0):
        if language == "JavaScript" and "NSScreenNumber" in script:
            return "Mac mini|1" + ("\nSmartTV 4K FFM|7" if self.tv else "")
        if language == "JavaScript":
            return MAC + ("\n" + TV if self.tv and not getattr(self, "hidden", False) else "")
        self.scripts.append(script)
        if "controlcenter-screen-mirroring" in script:
            if "-- stop mirroring" in script:                                 # the stop script
                self.tv = False
            elif self.extend_works:
                self.tv = True
            return "ok"
        for app in ("Citrix Viewer", "Other App"):
            if f'process "{app}"' in script:
                self.moves.append(("away" if "AXFullScreen\" of win to false" in script else "tv", app))
                if app not in self.running:
                    return "not running"
                if app in getattr(self, "windowless", ()):
                    return "no windows"
                self.moved.append(app)
                return "moved"
        return ""


class FakeCg:
    """CoreGraphics stand-in: the Mac's main display is 1, the TV (when online) is 7, and mirror_of says what mirrors what."""

    def __init__(self, mac, refuses=False, ignores=False):
        self.mac, self.mirrors, self.refuses, self.ignores, self.changes = mac, {}, refuses, ignores, []

    def main_display(self):
        return 1

    def online_displays(self):
        return [1, 7] if self.mac.tv else [1]

    def mirror_of(self, display):
        return self.mirrors.get(display, 0)

    def set_mirror(self, display, master):
        if self.refuses:
            raise DisplayConfigError("no")
        self.changes.append((display, master))
        if not self.ignores:
            self.mirrors[display] = master
            if display == 7 and master:                       # a mirroring display leaves macOS's own screen list
                self.mac.hidden = True
            elif display == 7:
                self.mac.hidden = False


def test_extend_and_stop_phrases():
    assert parse("extend my screen to the TV").kind == "extend_display"
    assert parse("Extend the display").kind == "extend_display"
    assert parse("bring my work in").kind == "mirror_display"
    assert parse("can you bring the screen here please").kind == "mirror_display"
    assert parse("put my work on the TV").kind == "mirror_display"
    assert parse("mirror my screen to the TV").kind == "mirror_display"
    assert parse("switch back to TV").kind == "stop_extending"
    assert parse("go back to the television").kind == "stop_extending"
    assert parse("stop extending the screen").kind == "stop_extending"
    assert parse("go back").kind == "key" and parse("go back").key == "BACK"          # a plain "go back" is still the Back key
    assert parse("mirror the moon") is None                                             # not on the list: refused, not guessed
    assert parse("bring me a drink") is None


def test_the_model_may_ask_for_a_screen_step_only_on_its_own():
    cmds, _ = validate({"steps": [{"op": "extend_display"}]}, DEFAULT_APPS)
    assert [c.kind for c in cmds] == ["extend_display"]
    cmds, _ = validate({"steps": [{"op": "mirror_display"}]}, DEFAULT_APPS)
    assert [c.kind for c in cmds] == ["mirror_display"]
    cmds, _ = validate({"steps": [{"op": "stop_extending"}]}, DEFAULT_APPS)
    assert [c.kind for c in cmds] == ["stop_extending"]
    assert validate({"steps": [{"op": "extend_display"}, {"op": "key", "key": "HOME"}]}, DEFAULT_APPS) is None
    assert parse_many("bring my work in and mute") is None                              # a chain never includes a screen step


def test_screen_lines_are_parsed():
    assert parse_screens(f"{MAC}\n{TV}\nnonsense\nbad|x|y|z|w") == [("Mac mini", 0, 0, 2560, 1440), ("SmartTV 4K FFM", 2560, 0, 1920, 1080)]


def test_names_that_could_escape_the_script_are_refused():
    for bad in ('TV" & (do shell script "x") & "', "TV\\", "TV\nkey code", "", "x" * 65, "C0 92"):
        with pytest.raises(DisplayError):
            extend_script(bad)
        with pytest.raises(DisplayError):
            stop_script(bad)
    with pytest.raises(DisplayError):
        move_script('App" & quit & "', 0, 0, 100, 100)
    assert "screen-mirroring-device-AirPlay:C0:92:1C:67:48:2E" in extend_script(ID)


async def test_extend_clicks_through_control_center_then_checks_the_screen_really_appeared():
    mac = FakeMac()
    said = await DisplayClient(mac, wait_s=1).extend("SmartTV 4K FFM", ["Citrix Viewer"], ID)
    assert mac.tv and mac.moved == ["Citrix Viewer"]
    assert said == "Your screen is on the TV, with Citrix Viewer"
    assert any("Use As Separate Display" in s for s in mac.scripts)
    move = next(s for s in mac.scripts if 'process "Citrix Viewer"' in s)
    assert "set position of win to {2560, 0}" in move and "set size of win to {1920, 1080}" in move   # it fills the TV, which starts at x 2560
    assert 'set value of attribute "AXFullScreen" of win to true' in move
    leave, place, enter = (move.index(k) for k in ('"AXFullScreen" of win to false', "set position of win", '"AXFullScreen" of win to true'))
    assert leave < place < enter                                                       # leave full screen, move, then go full screen on the TV
    assert 'subrole of win) is "AXStandardWindow"' in move                                   # dialogs are left alone


async def test_an_app_that_is_not_running_is_skipped_and_an_extended_tv_is_not_clicked_again():
    mac = FakeMac(tv_present=True, running=())
    said = await DisplayClient(mac, wait_s=1).extend("SmartTV 4K FFM", ["Citrix Viewer"], ID)
    assert said == "Your screen is on the TV"
    assert not any("controlcenter-screen-mirroring" in s for s in mac.scripts)         # already extended: nothing was clicked


async def test_when_the_click_does_not_produce_a_screen_it_says_so_and_does_not_pretend():
    with pytest.raises(DisplayError, match="could not extend"):
        await DisplayClient(FakeMac(extend_works=False), wait_s=1).extend("SmartTV 4K FFM", device_id=ID)
    with pytest.raises(DisplayError, match="No TV screen name"):
        await DisplayClient(FakeMac()).extend("")


async def test_stop_releases_the_screen_and_does_nothing_when_there_is_none():
    mac = FakeMac(tv_present=True)
    assert await DisplayClient(mac, wait_s=1).stop("SmartTV 4K FFM", ID) == "The TV is back to being a TV"
    assert not mac.tv
    quiet = FakeMac()
    assert await DisplayClient(quiet).stop("SmartTV 4K FFM", ID) == "The TV was not extended"
    assert quiet.scripts == []


class FakeTv:
    def __init__(self):
        self.sent = []

    async def run(self, cmd):
        self.sent.append(cmd)
        return cmd.label


def make_mode(mac, **tv):
    spoken, seen = [], []

    class Speaker:
        def enabled_for(self, mode):
            return True

        async def speak(self, text):
            spoken.append(text)

    bus = EventBus()
    real = bus.publish
    bus.publish = lambda type, **data: (seen.append((type, data)), real(type, **data))[1]
    tv.setdefault("ai", False)
    mode = TvControlMode(ModeContext(Config({"tv": tv}), bus, Speaker()))
    mode.client, mode.display = FakeTv(), DisplayClient(mac, wait_s=1)
    return mode, spoken, seen


async def test_bring_my_work_in_mirrors_the_main_screen_and_moves_nothing():
    mac = FakeMac()
    mode, spoken, _ = make_mode(mac, airplay_name="SmartTV 4K FFM", airplay_id=ID)
    cg = mode.display.cg = FakeCg(mac)
    await mode.handle("bring my work in")
    assert mac.tv and cg.changes == [(7, 1)] and mac.moved == []
    assert spoken == ["Your main screen is on the TV"]
    assert mode.client.sent == []                                                       # nothing is sent to the TV itself


async def test_switch_back_ends_a_mirror_before_it_disconnects():
    mac = FakeMac()
    mode, spoken, _ = make_mode(mac, airplay_name="SmartTV 4K FFM", airplay_id=ID)
    cg = mode.display.cg = FakeCg(mac)
    await mode.handle("bring my work in")
    await mode.handle("switch back to TV")
    assert cg.changes == [(7, 1), (7, 0)] and not mac.tv
    assert spoken[-1] == "The TV is back to being a TV"


async def test_a_mirror_macos_refuses_or_ignores_is_reported_not_claimed():
    for kwargs in ({"refuses": True}, {"ignores": True}):
        mac = FakeMac()
        mode, spoken, _ = make_mode(mac, airplay_name="SmartTV 4K FFM", airplay_id=ID)
        mode.display.cg = FakeCg(mac, **kwargs)
        mode.display.wait_s = 1
        await mode.handle("bring my work in")
        assert spoken and "main screen" in spoken[0] and spoken[0] != "Your main screen is on the TV"


async def test_switch_back_to_tv_releases_the_screen_and_sends_the_tv_home():
    mac = FakeMac(tv_present=True)
    mode, spoken, _ = make_mode(mac, airplay_name="SmartTV 4K FFM", airplay_id=ID)
    await mode.handle("switch back to TV")
    assert not mac.tv
    assert [c.key for c in mode.client.sent] == ["HOME"]
    assert spoken == ["The TV is back to being a TV"]


async def test_a_screen_failure_is_spoken_and_nothing_else_happens():
    mode, spoken, seen = make_mode(FakeMac(extend_works=False), airplay_name="SmartTV 4K FFM", airplay_id=ID)
    await mode.handle("extend my screen to the tv")
    assert spoken == ["I could not extend the screen to the TV. Check that its AirPlay is on."]
    assert any(t == "notice" for t, _ in seen)
    mode, spoken, _ = make_mode(FakeMac())
    await mode.handle("bring my work in")
    assert "No TV screen name" in spoken[0]


def test_failures_are_explained_honestly_and_a_missing_control_is_not_blamed_on_permissions():
    from voicectl.tv.display import explain
    assert "find Screen Mirroring" in explain("1228:1254: execution error: no screen mirroring tile (-2700)")
    assert "cannot see the TV" in explain("execution error: tv not listed (-2700)")
    assert "Accessibility" in explain("osascript is not allowed assistive access. (-25211)")
    assert "Accessibility" not in explain("something else broke")


def test_the_extend_script_searches_every_window_and_reopens_a_panel_that_closes_itself():
    script = extend_script(ID)
    assert "repeat with w in windows" in script                          # not only window 1
    assert "on panelOpen()" in script and "if not my panelOpen() then" in script       # Control Center closes itself after a few seconds
    assert script.count("my openControlCenter()") >= 1 and "repeat 6 times" in script     # bounded: it never loops for ever
    assert "Use As Separate Display" in script and "Use As Separate Display" not in stop_script(ID)
    assert "-- stop mirroring" in stop_script(ID) and "-- stop mirroring" not in script


def test_no_script_ever_sends_a_keystroke():
    """A keystroke goes to whichever app is in front: a terminal reads Escape as an interrupt, a document would be typed into."""
    for script in (extend_script(ID), stop_script(ID), move_script("Citrix Viewer", 0, 0, 100, 100)):
        assert "key code" not in script and "keystroke" not in script
    assert "on closePanel()" in extend_script(ID)


def test_the_lookup_reads_the_panel_into_a_list_before_looping_over_it():
    """Looping straight over `entire contents of w` found nothing on macOS 26; reading it into a list first works."""
    script = extend_script(ID)
    assert "set els to entire contents of w" in script and "repeat with e in els" in script
    assert "repeat with e in (entire contents of w)" not in script


def test_only_the_configured_device_can_ever_be_clicked():
    """The Screen Mirroring list also holds an iPad (Sidecar) and any other AirPlay device: none of them may ever be connected."""
    script = extend_script(ID)
    assert script.count("screen-mirroring-device-") == 1                                   # one device id in the whole script...
    assert 'set deviceId to "screen-mirroring-device-AirPlay:C0:92:1C:67:48:2E"' in script  # ...and it is exactly the configured one
    assert "Sidecar" not in script and 'contains "screen-mirroring-device' not in script   # no prefix or substring match
    assert "tell application \"System Events\" to click tv" in script
    assert script.count("click tv") == 1 and "if tvOn is not 1 then" in script           # only when the toggle is off
    by_id = script[script.index("on findById(wanted)"):script.index("end findById")]
    assert "name of e" not in by_id and "description of e" not in by_id                      # the device lookup compares the identifier only
    assert "set tv to my findById(deviceId)" in script and "findControl(deviceId)" not in script
    other = extend_script("AA:BB:CC:DD:EE:FF")
    assert "C0:92:1C:67:48:2E" not in other and "AirPlay:AA:BB:CC:DD:EE:FF" in other


async def test_with_no_device_id_nothing_is_clicked_and_no_other_device_is_chosen():
    mac = FakeMac()
    with pytest.raises(DisplayError, match="AirPlay ID"):
        await DisplayClient(mac, wait_s=1).extend("SmartTV 4K FFM", [], "")
    assert mac.scripts == []                                                                 # not even a lookup ran
    mode, spoken, _ = make_mode(FakeMac(), airplay_name="SmartTV 4K FFM")                    # name set, id not
    await mode.handle("bring my work in")
    assert "AirPlay ID" in spoken[0]


def test_macos_names_an_airplay_screen_with_a_suffix():
    from voicectl.tv.display import find_screen
    screens = [("OMEN 27qs", 0, 0, 2560, 1440), ("SmartTV 4K FFM (AirPlay)", 2560, 0, 1920, 1080)]
    assert find_screen(screens, "SmartTV 4K FFM")[0] == "SmartTV 4K FFM (AirPlay)"
    assert find_screen(screens, "smarttv 4k ffm (airplay)") is not None
    assert find_screen(screens, "SmartTV 4K") is None                    # a different, longer name is not the same screen


async def test_switch_back_brings_the_work_home_before_the_tv_goes_away():
    mac = FakeMac(tv_present=True)
    await DisplayClient(mac, wait_s=1).stop("SmartTV 4K FFM", ID, ["Citrix Viewer"])
    assert mac.moves == [("away", "Citrix Viewer")]
    home = next(s for s in mac.scripts if 'process "Citrix Viewer"' in s)
    assert 'set value of attribute "AXFullScreen" of win to false' in home and "{40, 60}" in home      # leaves full screen, back on the first screen
    assert mac.scripts.index(home) < next(i for i, s in enumerate(mac.scripts) if "-- stop mirroring" in s)   # work first, then the TV


async def test_it_does_not_claim_to_have_moved_an_app_that_has_no_window():
    mac = FakeMac(tv_present=True)
    mac.windowless = {"Citrix Viewer"}
    said = await DisplayClient(mac, wait_s=1).extend("SmartTV 4K FFM", ["Citrix Viewer"], ID)
    assert "with Citrix Viewer" not in said and "Citrix Viewer has no open window" in said
    assert mac.moved == []
    script = move_script("Citrix Viewer", 0, 0, 100, 100)
    assert "set movedCount to movedCount + 1" in script and 'return "no windows"' in script


def test_a_minimized_work_window_is_brought_back_before_it_is_moved_to_the_tv():
    to_tv, back = move_script("Citrix Viewer", 0, 0, 100, 100), move_script("Citrix Viewer", 0, 0, 100, 100, fullscreen=False)
    assert 'tell application "Citrix Viewer" to reopen' in to_tv
    assert to_tv.index("exists process") < to_tv.index("reopen")        # never launches an app that is not running
    assert "reopen\n" not in back and 'to reopen' not in back


def test_a_window_only_counts_as_moved_if_it_is_read_back_on_the_target_screen():
    script = move_script("Citrix Viewer", 2560, 0, 1920, 1080)
    assert "set winPos to position of win" in script
    assert "(item 1 of winPos) >= 2552" in script and "(item 1 of winPos) < 4480" in script       # inside x 2560..4480 (with a small margin)
    assert script.index("set movedCount to movedCount + 1") > script.index("set winPos to position of win")


def test_connect_clicks_only_when_off_and_release_clicks_only_when_on():
    assert "if tvOn is not 1 then" in extend_script(ID) and "if tvOn is 1 then" not in extend_script(ID)
    assert "if tvOn is 1 then" in stop_script(ID) and "if tvOn is not 1 then" not in stop_script(ID)


def test_switch_back_only_touches_windows_that_are_on_the_tv():
    back = move_script("Citrix Viewer", 0, 0, 2560, 1440, fullscreen=False, only_within=(-1920, 0, 1920, 1080))
    assert "(item 1 of (position of win)) >= -1928" in back and "(item 1 of (position of win)) < 0" in back
    assert "(item 2 of (position of win)) >= -8" in back and "(item 2 of (position of win)) < 1080" in back
    assert "AXStandardWindow\" and (" in back
    assert "and (true)" in move_script("Citrix Viewer", 0, 0, 100, 100)           # moving TO the TV has no such condition
