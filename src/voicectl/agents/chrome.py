from __future__ import annotations

"""Drive the user's REAL Google Chrome through its AppleScript support.

Why: logins (LinkedIn etc.) already live in that Chrome, and no second browser or window appears. Chrome 136+ refuses
remote debugging on the default profile, so AppleScript + page JavaScript is the way to operate the browser you
actually use. Prometheus works in ONE dedicated tab of your frontmost Chrome window and reuses it.

One-time setup in Chrome: menu View > Developer > Allow JavaScript from Apple Events. macOS will also ask once whether
Voice Control may control Chrome (System Settings > Privacy & Security > Automation).
"""

import asyncio
import json

from voicectl import platform

from voicectl.agents.browser import READ_JS, click_js, format_page, media_js, press_js, scroll_js, type_js

JS_OFF_HELP = ("SETUP NEEDED: Chrome isn't allowing remote control yet (its 'Allow JavaScript from Apple Events' setting is off). "
               "Tell the user in one plain sentence, ask if you may switch it on for them, and if they agree call chrome_setup. "
               "Do NOT work around this with Bash, osascript or 'open'; those cannot click or type.")
AUTOMATION_HELP = ("SETUP NEEDED: macOS hasn't allowed Voice Control to control Chrome. Tell the user in one plain sentence to open "
                   "System Settings > Privacy & Security > Automation and switch on Google Chrome under Voice Control. "
                   "Do NOT work around this with Bash, osascript or 'open'.")


class ChromeError(RuntimeError):
    pass


SHOW_MENU = '''
tell application "%(app)s" to activate
delay 0.5
tell application "System Events"
  tell process "%(app)s"
    set viewItem to menu bar item "View" of menu bar 1
    click viewItem
    delay 0.4
    click menu item "Developer" of menu 1 of viewItem
  end tell
end tell
'''


ENABLE_APPLE_EVENTS = '''
on run argv
  tell application "%(app)s" to activate
  delay 0.5
  tell application "System Events"
    tell process "%(app)s"
      set devMenu to menu 1 of menu item "Developer" of menu 1 of menu bar item "View" of menu bar 1
      set mi to menu item "Allow JavaScript from Apple Events" of devMenu
      set mark to value of attribute "AXMenuItemMarkChar" of mi
      if mark is missing value then
        click mi
        return "ENABLED"
      end if
      return "ALREADY"
    end tell
  end tell
end run
'''


FIND_TAB = """
on run argv
  set tid to (item 1 of argv) as text
  tell application "%(app)s"
    repeat with w in windows
      repeat with t in tabs of w
        if ((id of t) as text) is tid then return "FOUND"
      end repeat
    end repeat
  end tell
  return "NONE"
end run
"""

NEW_TAB = """
on run argv
  tell application "%(app)s"
    if (count of windows) = 0 then make new window
    set t to make new tab at end of tabs of front window with properties {URL:"about:blank"}
    return (id of t) as text
  end tell
end run
"""

RUN_JS = """
on run argv
  set tid to (item 1 of argv) as text
  set js to item 2 of argv
  tell application "%(app)s"
    repeat with w in windows
      repeat with t in tabs of w
        if ((id of t) as text) is tid then return (execute t javascript js)
      end repeat
    end repeat
  end tell
  error "Prometheus's Chrome tab was closed"
end run
"""

NAVIGATE = """
on run argv
  set tid to (item 1 of argv) as text
  set u to item 2 of argv
  tell application "%(app)s"
    repeat with w in windows
      repeat with t in tabs of w
        if ((id of t) as text) is tid then
          set URL of t to u
          return "OK"
        end if
      end repeat
    end repeat
  end tell
  error "Prometheus's Chrome tab was closed"
end run
"""

IS_LOADING = """
on run argv
  set tid to (item 1 of argv) as text
  tell application "%(app)s"
    repeat with w in windows
      repeat with t in tabs of w
        if ((id of t) as text) is tid then return (loading of t) as text
      end repeat
    end repeat
  end tell
  return "false"
end run
"""


class ChromeBrowser:
    def __init__(self, app: str = "Google Chrome", screen=None) -> None:
        self.app = app
        self.screen = screen          # voicectl.agents.screen.Screen, for screenshots
        self.tab_id = 0
        self._unused_tab = False
        self.last_url = ""
        self.last_title = ""

    # ---- plumbing ----
    async def _osa(self, script: str, *args: str, timeout: float = 25.0) -> str:
        try:
            code, out, err = await platform.current().osascript_async(script % {"app": self.app}, *args, timeout=timeout)
        except asyncio.TimeoutError:
            raise ChromeError("Chrome didn't answer in time.")
        if code != 0:
            msg = err.strip()
            if "turned off" in msg or "JavaScript through AppleScript" in msg:
                raise ChromeError(JS_OFF_HELP)
            if "-1743" in msg or "Not authorized" in msg:
                raise ChromeError(AUTOMATION_HELP)
            raise ChromeError(msg or "Chrome control failed.")
        return out.rstrip("\n")

    async def _tab(self) -> int:
        """The one tab Prometheus works in. Created on first use in the front window; reused afterwards."""
        if self.tab_id and await self._osa(FIND_TAB, str(self.tab_id)) == "FOUND":
            return self.tab_id
        self.tab_id = int(await self._osa(NEW_TAB))
        self._unused_tab = True                 # blank until we navigate somewhere
        return self.tab_id

    async def _js(self, js: str) -> str:
        tid = await self._tab()
        try:
            return await self._osa(RUN_JS, str(tid), js)
        except ChromeError:
            if self._unused_tab:                # we just made a blank tab and can't use it: don't leave litter in the user's Chrome
                try:
                    await self.close_tab()
                except ChromeError:
                    pass
            raise

    async def _settle(self, extra: float = 0.4) -> None:
        """Wait for navigation/rendering to calm down."""
        tid = str(await self._tab())
        for _ in range(60):
            if await self._osa(IS_LOADING, tid) != "true":
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(extra)

    async def enable_apple_events(self) -> str:
        """Try to switch on Chrome's 'Allow JavaScript from Apple Events'. Chrome 154 ignores programmatic clicks on this
        one switch, so when that happens we open the right menu for the user and say exactly what to click."""
        try:
            await self._osa(ENABLE_APPLE_EVENTS, timeout=30)
            await asyncio.sleep(1.0)
            await self._js("1 + 1")                                 # prove it works before saying it does
            return "Done: Chrome now allows me to control it."
        except ChromeError as first:
            problem = str(first)
        try:
            await self._osa(SHOW_MENU, timeout=20)                   # leave View > Developer open, ready for one click
            opened = "I've opened Chrome's View menu at Developer for you."
        except ChromeError:
            opened = "In Chrome open the View menu, then Developer."
        raise ChromeError(f"I couldn't switch it on myself: Chrome ignores that click. {opened} The user needs to click "
                          "'Allow JavaScript from Apple Events' (one time) and then tell you they've done it. Say that in one "
                          f"plain sentence and wait; do not call chrome_setup again until they say it's done. ({problem[:80]})")

    # ---- the backend interface ----
    async def read(self) -> str:
        raw = await self._js(READ_JS)
        d = json.loads(raw)
        self.last_url, self.last_title = d["url"], d["title"]
        return format_page(raw)

    async def goto(self, url: str) -> str:
        tid = await self._tab()
        await self._osa(NAVIGATE, str(tid), url if "://" in url else f"https://{url}")
        self._unused_tab = False                # it now shows a page: keep it
        await self._settle(0.8)
        return await self.read()

    async def click(self, index: int) -> str:
        if await self._js(click_js(index)) == "MISSING":
            raise LookupError("No such element; call browser_read again.")
        await self._settle(0.8)
        return await self.read()

    async def type(self, index: int, text: str, submit: bool) -> str:
        if await self._js(type_js(index, text, submit)) == "MISSING":
            raise LookupError("No such element; call browser_read again.")
        await self._settle(1.0 if submit else 0.3)
        return await self.read()

    async def press(self, key: str) -> str:
        await self._js(press_js(key))
        await self._settle(0.5)
        return await self.read()

    async def scroll(self, direction: str, amount: float) -> str:
        if await self._js(scroll_js(direction, amount)) == "BAD_DIRECTION":
            raise ValueError("direction must be down, up, top or bottom")
        await asyncio.sleep(0.5)
        return await self.read()

    async def back(self) -> str:
        await self._js("history.back(); 'OK'")
        await self._settle(0.8)
        return await self.read()

    async def forward(self) -> str:
        await self._js("history.forward(); 'OK'")
        await self._settle(0.8)
        return await self.read()

    def has_page(self) -> bool:
        return bool(self.tab_id)

    async def media(self, action: str) -> str:
        return await self._js(media_js(action))

    async def screenshot(self) -> tuple[bytes, str] | None:
        if self.screen is None:
            return None
        data, mime, _ = await self.screen.look("chrome")        # via the app, which holds Screen Recording permission
        return data, mime

    async def close_tab(self) -> None:
        """Close Prometheus's own tab (never any other)."""
        if self.tab_id:
            await self._osa('on run argv\ntell application "%(app)s"\nrepeat with w in windows\nrepeat with t in tabs of w\n'
                            'if ((id of t) as text) is (item 1 of argv as text) then close t\nend repeat\nend repeat\nend tell\nend run', str(self.tab_id))
            self.tab_id = 0

    async def close(self) -> None:
        pass                           # it's the user's Chrome: leave it (and the tab) alone
