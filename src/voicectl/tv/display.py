"""Extend this Mac's screen to the TV (AirPlay "Use as Separate Display") and bring chosen work windows onto it, or take it away again.

Fixed scripts only. The one thing that varies is the TV's AirPlay name, which comes from the settings and is checked against a short
list of safe characters before it goes into a script; nothing a person says is ever put into one. Success is never assumed from
a click: it is "a display with the TV's name is now there" (or gone), read back from macOS.

The Control Center script drives macOS's own menus through UI scripting, so it needs Accessibility permission for the app and can
break when Apple redesigns those menus. When it cannot do the job it says so (DisplayError) instead of guessing.
"""
from __future__ import annotations

import asyncio
import logging
import re

from voicectl import platform
from voicectl.platform import displays as macos_displays

log = logging.getLogger(__name__)

SAFE_NAME = re.compile(r"[A-Za-z0-9 .'’()+-]{1,40}")
SAFE_ID = re.compile(r"[A-Za-z0-9:._-]{1,64}")
SAFE_APP = re.compile(r"[A-Za-z0-9 .+-]{1,40}")
CONNECT_WAIT_S = 12
SCRIPT_TIMEOUT_S = 40


class DisplayError(Exception):
    """The screen could not be extended or released. The message is safe to say aloud."""


def explain(err: str) -> str:
    """What a failed script means, in words that are safe to say aloud."""
    e = err.lower()
    if "no screen mirroring tile" in e:
        return "I could not find Screen Mirroring in Control Center."
    if "tv not listed" in e:
        return "I cannot see the TV in the Screen Mirroring list. Check that its AirPlay is on and it is on the same network."
    if "assistive access" in e or "-25211" in e or "-1719" in e or "not authorized" in e or "-1743" in e:
        return "macOS would not let me do that. Allow Prometheus TV under Accessibility in System Settings."
    return "macOS did not let me finish that."


async def osascript(script: str, language: str | None = None, timeout: float = SCRIPT_TIMEOUT_S) -> str:
    """Run one script through the platform layer and return what it printed. Replaced by a fake in tests."""
    try:
        code, out, err = await platform.current().osascript_async(script, timeout=timeout, language=language)
    except asyncio.TimeoutError:
        raise DisplayError("The Mac took too long to answer.") from None
    if code != 0:
        log.warning("osascript failed: %s", err.strip()[:300])
        raise DisplayError(explain(err))
    return out.strip()


# What is attached: one line per screen, "name|x|y|w|h", in the top-left origin that window scripting uses.
LIST_SCREENS_JXA = """
ObjC.import('AppKit');
const screens = $.NSScreen.screens;
const primaryH = screens.objectAtIndex(0).frame.size.height;
const rows = [];
for (let i = 0; i < screens.count; i++) {
  const s = screens.objectAtIndex(i), f = s.frame;
  rows.push([ObjC.unwrap(s.localizedName), Math.round(f.origin.x), Math.round(primaryH - (f.origin.y + f.size.height)),
             Math.round(f.size.width), Math.round(f.size.height)].join('|'));
}
rows.join('\\n');
"""


# Which id each screen has, "name|id": the id is what CoreGraphics (and so mirroring) uses.
LIST_IDS_JXA = """
ObjC.import('AppKit');
const screens = $.NSScreen.screens;
const rows = [];
for (let i = 0; i < screens.count; i++) {
  const s = screens.objectAtIndex(i);
  rows.push(ObjC.unwrap(s.localizedName) + '|' + ObjC.unwrap(s.deviceDescription.objectForKey('NSScreenNumber')));
}
rows.join('\\n');
"""


def parse_ids(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        name, _, ident = line.rpartition("|")
        if name and ident.strip().isdigit():
            out[name] = int(ident)
    return out


def parse_screens(text: str) -> list[tuple[str, int, int, int, int]]:
    out = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) == 5:
            try:
                out.append((parts[0], *(int(p) for p in parts[1:])))
            except ValueError:
                continue
    return out


def find_screen(screens: list[tuple[str, int, int, int, int]], name: str):
    """The screen called `name`. macOS names an AirPlay screen "<name> (AirPlay)", so the name may be followed by that suffix."""
    want = name.casefold()
    return next((s for s in screens if s[0].casefold() == want or s[0].casefold() == f"{want} (airplay)"), None)


def extend_script(device_id: str) -> str:
    """Open Control Center, Screen Mirroring, pick the TV and choose "Use as Separate Display". The TV is found by its AirPlay device
    id (the list shows no names), every control by accessibility identifier, never by position, in every window of Control Center. The panel closes itself after a few
    seconds, so every step first checks it is still open and opens it again (and goes back to the TV list) when it is not."""
    if not SAFE_ID.fullmatch(device_id):
        raise DisplayError("No TV AirPlay ID is set up, or it has characters I will not use. Put it in the settings.")
    return f'''
on panelOpen()
  tell application "System Events" to tell process "ControlCenter"
    return (count of windows) > 0
  end tell
end panelOpen

on openControlCenter()
  tell application "System Events" to tell process "ControlCenter"
    click (first menu bar item of menu bar 1 whose description is "Control Center")
  end tell
end openControlCenter

on closePanel()
  -- Never a key press: a key goes to whichever app is in front (a terminal reads Escape as an interrupt). Clicking Control Center's own
  -- menu bar item again closes its panel.
  repeat 2 times
    if my panelOpen() then
      my openControlCenter()
      delay 0.5
    end if
  end repeat
end closePanel

on findControl(wanted)
  tell application "System Events" to tell process "ControlCenter"
    repeat with w in windows
      try
        set els to entire contents of w          -- read into a list first: looping over the live expression finds nothing
        repeat with e in els
          try
            set i to ""
            try
              set i to value of attribute "AXIdentifier" of e
            end try
            set n to ""
            try
              set n to name of e
            end try
            set d to ""
            try
              set d to description of e
            end try
            if i is wanted or n is wanted or d is wanted then return e
          end try
        end repeat
      end try
    end repeat
  end tell
  return missing value
end findControl

on findById(wanted)
  -- Identifier only, never a name or description: used for every control that connects or disconnects a device, so that
  -- nothing can match by accident and only the one configured device is ever clicked.
  tell application "System Events" to tell process "ControlCenter"
    repeat with w in windows
      try
        set els to entire contents of w
        repeat with e in els
          try
            set i to ""
            try
              set i to value of attribute "AXIdentifier" of e
            end try
            if i is wanted then return e
          end try
        end repeat
      end try
    end repeat
  end tell
  return missing value
end findById

set deviceId to "screen-mirroring-device-AirPlay:{device_id}"
set tv to missing value
set sawTile to false
repeat 6 times
  set tv to my findById(deviceId)
  if tv is not missing value then exit repeat
  if not my panelOpen() then
    my openControlCenter()
    delay 0.7
  else
    set tile to my findById("controlcenter-screen-mirroring")
    if tile is not missing value then
      set sawTile to true
      tell application "System Events" to click tile
      delay 1.0
    else
      delay 0.5
    end if
  end if
end repeat
if tv is missing value then
  my closePanel()
  if sawTile then error "tv not listed"
  error "no screen mirroring tile"
end if
-- The entry is a toggle: clicking it while connected would disconnect. Only click it when it is off.
tell application "System Events" to set tvOn to value of tv
if tvOn is not 1 then
  tell application "System Events" to click tv
end if
delay 1.5
set sep to missing value
repeat 6 times
  if not my panelOpen() then exit repeat
  set sep to my findControl("Use As Separate Display")
  if sep is not missing value then exit repeat
  delay 0.4
end repeat
if sep is not missing value then
  tell application "System Events" to click sep
end if
delay 0.6
my closePanel()
return "ok"
'''


def stop_script(device_id: str) -> str:
    """Open Screen Mirroring and press the TV once more, which turns the connected display off."""
    script = extend_script(device_id).replace('set sep to my findControl("Use As Separate Display")', 'set sep to missing value')
    # The connect script clicks the TV only when it is OFF; releasing it must click only when it is ON.
    script = script.replace("if tvOn is not 1 then", "if tvOn is 1 then").replace("only click it when it is off", "only click it when it is on")
    return "-- stop mirroring\n" + script


def move_script(app: str, x: int, y: int, w: int, h: int, fullscreen: bool = True, only_within: tuple[int, int, int, int] | None = None) -> str:
    """Put every standard window of one running app on the screen whose top-left corner is (x, y): sized to fill it, and (on the TV)
    in macOS full screen, so the TV shows just that app. Dialogs and other non-standard windows are left alone. With fullscreen
    False the window leaves full screen first and is placed with a margin, which is how it is brought back to a normal screen."""
    if not SAFE_APP.fullmatch(app):
        raise DisplayError("A work app's name in the settings has characters I will not use.")
    if fullscreen:
        # A full-screen window ignores position changes: leave full screen, move it, then go full screen on the TV.
        place = f"""try
            set value of attribute "AXFullScreen" of win to false
          end try
          delay 1.4
          set position of win to {{{x}, {y}}}
          set size of win to {{{w}, {h}}}
          delay 0.4
          try
            set value of attribute "AXFullScreen" of win to true
          end try
          delay 1.6"""
    else:
        place = f"""try
            set value of attribute "AXFullScreen" of win to false
          end try
          delay 0.8
          set position of win to {{{x + 40}, {y + 60}}}
          set size of win to {{{max(w - 80, 400)}, {max(h - 120, 300)}}}"""
    # The same "reopen" a Dock click sends: it brings back a minimized window. Only for a running app, and only when moving to the TV.
    reopen = f'''try
    tell application "{app}" to reopen
  end try
  delay 1.0''' if fullscreen else "-- (no reopen: the window is being brought back, not fetched)"
    if only_within:        # only a window that is on that screen right now: one that never left the main screen is left exactly as it is
        fx, fy, fw, fh = only_within
        guard = (f"(item 1 of (position of win)) >= {fx - 8} and (item 1 of (position of win)) < {fx + fw} "
                 f"and (item 2 of (position of win)) >= {fy - 8} and (item 2 of (position of win)) < {fy + fh}")
    else:
        guard = "true"
    return f'''
tell application "System Events"
  if not (exists process "{app}") then return "not running"
  {reopen}
  set movedCount to 0
  tell process "{app}"
    repeat with win in windows
      try
        if (subrole of win) is "AXStandardWindow" and ({guard}) then
          {place}
          set winPos to position of win
          -- Counted only if it really is on that screen now, read back from the window itself.
          if (item 1 of winPos) >= {x - 8} and (item 1 of winPos) < {x + w} and (item 2 of winPos) >= {y - 8} and (item 2 of winPos) < {y + h} then
            set movedCount to movedCount + 1
          end if
        end if
      end try
    end repeat
  end tell
end tell
if movedCount > 0 then return "moved"
return "no windows"
'''


class DisplayClient:
    """The TV as a second screen of this Mac. `runner` is osascript, replaced by a fake in tests."""

    def __init__(self, runner=None, wait_s: float = CONNECT_WAIT_S, cg=None) -> None:
        self.nothing_to_move: list[str] = []
        self.cg = cg or macos_displays
        self.runner = runner or osascript
        self.wait_s = wait_s

    async def screens(self):
        return parse_screens(await self.runner(LIST_SCREENS_JXA, "JavaScript"))

    async def extend(self, name: str, work_apps: list[str] | None = None, device_id: str = "") -> str:
        """Extend the screen to the TV, then bring the work apps that are running onto it. Returns what to say."""
        if not name:
            raise DisplayError("No TV screen name is set up. Put the TV's AirPlay name in the settings.")
        if find_screen(await self.screens(), name) is None:
            await self.runner(extend_script(device_id))
            if not await self._wait_for(name, present=True):
                raise DisplayError("I could not extend the screen to the TV. Check that its AirPlay is on.")
        self.nothing_to_move = []
        moved = await self._bring_work(name, work_apps or [])
        said = "Your screen is on the TV" + (f", with {', '.join(moved)}" if moved else "")
        if self.nothing_to_move:
            said += f". {', '.join(self.nothing_to_move)} has no open window to bring"
        return said

    async def mirror_main(self, name: str, device_id: str = "") -> str:
        """Connect the TV, then make it a mirror of the main screen. Whatever is on the main screen (Citrix, full screen) is on the TV."""
        await self.extend(name, [], device_id)                       # connected first, with all of extend's checks
        tv = await self._tv_id(name)
        main = self.cg.main_display()
        if tv is None or tv == main:
            raise DisplayError("I could not find the TV among the screens to mirror.")
        try:
            self.cg.set_mirror(tv, main)
        except macos_displays.DisplayConfigError:
            raise DisplayError("macOS would not mirror the main screen to the TV.") from None
        if not await self._wait_mirroring(tv, main):
            raise DisplayError("The TV did not start mirroring the main screen.")
        return "Your main screen is on the TV"

    async def _tv_id(self, name: str) -> int | None:
        ids = parse_ids(await self.runner(LIST_IDS_JXA, "JavaScript"))
        want = name.casefold()
        return next((i for n, i in ids.items() if n.casefold() in (want, f"{want} (airplay)")), None)

    async def _wait_mirroring(self, tv: int, main: int) -> bool:
        waited = 0.0
        while waited <= min(self.wait_s, 6):
            if self.cg.mirror_of(tv) == main:
                return True
            await asyncio.sleep(0.4)
            waited += 0.4
        return False

    async def _unmirror(self, name: str) -> bool:
        """Stop any display mirroring the main screen, so the TV is an ordinary extra screen again. True if one was."""
        main = self.cg.main_display()
        mirroring = [d for d in self.cg.online_displays() if d != main and self.cg.mirror_of(d) == main]
        for display in mirroring:
            try:
                self.cg.set_mirror(display, 0)
            except macos_displays.DisplayConfigError:
                raise DisplayError("macOS would not stop mirroring the TV.") from None
        if mirroring:                                                # the screen comes back in macOS's list a moment later
            await self._wait_for(name, present=True)
        return bool(mirroring)

    async def stop(self, name: str, device_id: str = "", work_apps: list[str] | None = None) -> str:
        """Stop extending. Nothing happens, and nothing is clicked, when the TV is not a screen of this Mac."""
        if not name:
            raise DisplayError("No TV screen name is set up. Put the TV's AirPlay name in the settings.")
        await self._unmirror(name)                                    # mirroring hides the TV from macOS's screen list: end it first
        screens = await self.screens()
        if find_screen(screens, name) is None:
            return "The TV was not extended"
        await self._bring_back(work_apps or [], screens[0], find_screen(screens, name))   # the work comes home before its screen goes away
        await self.runner(stop_script(device_id))
        if not await self._wait_for(name, present=False):
            raise DisplayError("I could not release the TV screen. Disconnect it from the Screen Mirroring menu in the menu bar.")
        return "The TV is back to being a TV"

    async def _bring_work(self, name: str, apps: list[str]) -> list[str]:
        screen = find_screen(await self.screens(), name)
        if screen is None:
            return []
        _, x, y, w, h = screen
        moved = []
        for app in apps:
            try:
                result = await self.runner(move_script(app, x, y, w, h))
                if result == "moved":
                    moved.append(app)
                elif result == "no windows":
                    self.nothing_to_move.append(app)               # running, but with no window to put on the TV
                                                                   # ("not running" is simply skipped: nothing to say)
            except DisplayError:
                log.info("could not move %s", app)
        return moved

    async def _bring_back(self, apps: list[str], primary, tv) -> None:
        _, x, y, w, h = primary
        on_tv = tv[1:5]                                              # only windows that are on the TV come home
        for app in apps:
            try:
                await self.runner(move_script(app, x, y, w, h, fullscreen=False, only_within=on_tv))
            except DisplayError:
                log.info("could not bring %s back", app)

    async def _wait_for(self, name: str, present: bool) -> bool:
        waited = 0.0
        while waited <= self.wait_s:
            if (find_screen(await self.screens(), name) is not None) == present:
                return True
            await asyncio.sleep(0.5)
            waited += 0.5
        return False
