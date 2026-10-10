from __future__ import annotations

"""Drive the user's REAL Chrome through Google's official chrome-devtools-mcp server (Chrome DevTools Protocol).

Compared with injecting JavaScript: clicks and keystrokes are genuine browser input (trusted events), screenshots come straight
from Chrome (no macOS Screen Recording permission), pages are described by the accessibility tree, and waiting for
navigation is handled for us. Prometheus works in ONE tab it opens in your Chrome (no new window) and reuses it.

One-time setup in Chrome: open chrome://inspect/#remote-debugging and switch on Remote Debugging. Chrome may then show an
"Allow" dialog the first time something connects. Needs Node.js and `npm install -g chrome-devtools-mcp`.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from voicectl.agents.browser import media_js
from voicectl.core.config import DATA_DIR

log = logging.getLogger("voicectl.devtools")

SETUP_HELP = ("SETUP NEEDED: Chrome isn't open for remote control yet. Tell the user in one plain sentence: in Chrome, open "
              "chrome://inspect/#remote-debugging and switch on Remote Debugging (one time), and click Allow if Chrome asks. "
              "Offer to open that page for them by calling chrome_setup, then wait. "
              "Do NOT work around this with Bash, osascript or 'open'; those cannot click or type.")

INTERACTIVE = {"link", "button", "textbox", "searchbox", "combobox", "checkbox", "radio", "tab", "menuitem", "menuitemcheckbox",
               "menuitemradio", "option", "switch", "slider", "spinbutton", "treeitem", "textarea", "listbox"}
TEXTUAL = {"StaticText", "heading", "text", "paragraph", "ListMarker"}
MAX_TEXT, MAX_ELEMENTS = 3500, 80

_UID = re.compile(r'^\s*uid=(\S+)\s+(\S+)(?:\s+"((?:[^"\\]|\\.)*)")?(.*)$')
_URL = re.compile(r'\burl="([^"]*)"')
_PAGE = re.compile(r"^\s*(\d+):\s")


class DevToolsError(RuntimeError):
    pass


@dataclass
class Result:
    text: str
    images: list[tuple[bytes, str]] = field(default_factory=list)
    is_error: bool = False


def command() -> list[str] | None:
    """How to start the server, or None if Node / the package isn't installed."""
    node_dirs = ["/opt/homebrew/bin", "/usr/local/bin"]
    path = os.pathsep.join(node_dirs + [os.environ.get("PATH", "")])
    exe = shutil.which("chrome-devtools-mcp", path=path)
    return [exe, "--autoConnect", "--no-usage-statistics", "--no-performance-crux"] if exe and shutil.which("node", path=path) else None


class DevToolsClient:
    """A long-lived MCP session with the server. The contexts must be entered and left by ONE task, so a dedicated task owns
    them; everyone else just calls call()."""

    def __init__(self, cmd: list[str] | None = None) -> None:
        self.cmd = cmd or command()
        self._session = None
        self._runner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._stop = asyncio.Event()

    async def _own(self, ready: asyncio.Future) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {**os.environ, "PATH": os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")]),
               "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1", "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"}
        errlog = open(DATA_DIR / "devtools.log", "a")
        try:
            async with AsyncExitStack() as stack:
                params = StdioServerParameters(command=self.cmd[0], args=self.cmd[1:], env=env)
                read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), 60)
                self._session = session
                ready.set_result(True)
                await self._stop.wait()
        except BaseException as e:
            if not ready.done():
                ready.set_exception(DevToolsError(f"Couldn't start the Chrome DevTools server: {e}"))
        finally:
            self._session = None
            errlog.close()

    async def _ensure(self) -> None:
        if self._session is not None:
            return
        if not self.cmd:
            raise DevToolsError("Node.js or chrome-devtools-mcp isn't installed (brew install node; npm install -g chrome-devtools-mcp).")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._stop = asyncio.Event()
        self._ready = asyncio.get_running_loop().create_future()
        self._runner = asyncio.create_task(self._own(self._ready))
        await self._ready

    async def call(self, name: str, args: dict | None = None, timeout: float = 60.0) -> Result:
        await self._ensure()
        try:
            res = await asyncio.wait_for(self._session.call_tool(name, args or {}), timeout)
        except asyncio.TimeoutError:
            raise DevToolsError(f"Chrome didn't answer ({name}).") from None
        text = "".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
        images = [(base64.b64decode(c.data), getattr(c, "mimeType", "image/png")) for c in res.content if getattr(c, "type", "") == "image"]
        if res.is_error and ("DevToolsActivePort" in text or "Could not connect to Chrome" in text):
            raise DevToolsError(SETUP_HELP)
        return Result(text, images, bool(res.is_error))

    async def close(self) -> None:
        self._stop.set()
        if self._runner:
            await asyncio.gather(self._runner, return_exceptions=True)
        self._session = None


def strip_page_list(text: str) -> str:
    """Tool replies often append a list of ALL your open tabs. Never keep that: it is private and not needed."""
    return re.split(r"(?:^|\n)## Pages\b", text)[0].strip()


def parse_snapshot(text: str) -> tuple[str, str, str, list[dict]]:
    """-> (title, url, visible text, interactive elements [{n, uid, role, label, href}])"""
    title = url = ""
    texts: list[str] = []
    elements: list[dict] = []
    for line in strip_page_list(text).splitlines():
        m = _UID.match(line)
        if not m:
            continue
        uid, role, name, rest = m.group(1), m.group(2), (m.group(3) or "").replace('\\"', '"'), m.group(4)
        href = (_URL.search(rest) or [None, ""])[1]
        if role == "RootWebArea":
            title, url = name, href
        elif role in INTERACTIVE and len(elements) < MAX_ELEMENTS:
            elements.append({"n": len(elements) + 1, "uid": uid, "role": role, "label": name[:100], "href": href[:90]})
        elif role in TEXTUAL and name:
            texts.append(name)
    return title, url, "\n".join(texts)[:MAX_TEXT], elements


SCROLL_JS = """() => {
  const dir = %(direction)s, amount = %(amount)s;
  const scrollers = [document.scrollingElement, ...[...document.querySelectorAll('*')].filter(e => {
    const o = getComputedStyle(e).overflowY;
    return /(auto|scroll)/.test(o) && e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200 && e.clientWidth > 300;
  })];
  const el = scrollers.filter(e => e && e.scrollHeight > e.clientHeight + 50)
    .sort((a, b) => b.clientHeight * b.clientWidth - a.clientHeight * a.clientWidth)[0] || document.scrollingElement;
  const before = el.scrollTop, page = el.clientHeight * 0.85 * amount;
  const target = dir === 'down' ? before + page : dir === 'up' ? before - page : dir === 'top' ? 0 : el.scrollHeight;
  el.scrollTo({top: target, behavior: 'instant'});
  const after = el.scrollTop, max = Math.max(1, el.scrollHeight - el.clientHeight);
  return {moved: Math.abs(after - before) > 2, percent: Math.round(100 * after / max), atEnd: after >= max - 2, atStart: after <= 2};
}"""


class DevToolsBrowser:
    def __init__(self, client: DevToolsClient | None = None) -> None:
        self.client = client or DevToolsClient()
        self.page_id: int | None = None
        self._uids: dict[int, str] = {}
        self._labels: dict[int, tuple[str, str]] = {}
        self._shown = False
        self.last_url = ""
        self.last_title = ""

    def has_page(self) -> bool:
        return self.page_id is not None

    async def warm_up(self) -> None:
        await self.client._ensure()

    # ---- helpers ----
    @staticmethod
    def _page_ids(text: str) -> set[int]:
        return {int(m.group(1)) for line in text.splitlines() if (m := _PAGE.match(line))}

    async def _open(self, url: str) -> None:
        before = self._page_ids((await self.client.call("list_pages")).text)
        res = await self.client.call("new_page", {"url": url})
        new = self._page_ids(res.text) - before
        if res.is_error or not new:
            raise DevToolsError(f"Couldn't open a page: {strip_page_list(res.text)[:200]}")
        self.page_id, self._shown = max(new), False

    async def _ok(self, name: str, args: dict) -> Result:
        res = await self.client.call(name, {"pageId": self.page_id, **args})
        if res.is_error:
            if re.search(r"page.*(not found|closed|no longer)|No page", res.text, re.I):
                self.page_id = None                       # the user closed our tab: start a fresh one next time
            raise DevToolsError(strip_page_list(res.text)[:300] or f"{name} failed")
        return res

    async def _need_page(self) -> int:
        if self.page_id is None:
            await self._open("about:blank")
        return self.page_id

    # ---- the backend interface ----
    async def read(self) -> str:
        await self._need_page()
        res = await self._ok("take_snapshot", {})
        title, url, text, els = parse_snapshot(res.text)
        self.last_title, self.last_url = title, url
        self._uids = {e["n"]: e["uid"] for e in els}
        self._labels = {e["n"]: (e["role"], e["label"]) for e in els}
        lines = [f"[{e['n']}] {e['role']} {e['label']}" + (f" -> {e['href']}" if e["href"] else "") for e in els]
        return f"URL: {url}\nTITLE: {title}\n\nTEXT:\n{text}\n\nELEMENTS:\n" + "\n".join(lines)

    async def goto(self, url: str) -> str:
        url = url if "://" in url else f"https://{url}"
        if self.page_id is None:
            await self._open(url)
        else:
            await self._ok("navigate_page", {"type": "url", "url": url})
        if not self._shown:                                # show the user the tab once, so they can see what it does
            try:
                await self._ok("select_page", {"bringToFront": True})
            except DevToolsError:
                pass
            self._shown = True
        return await self.read()

    def _uid(self, index: int) -> str:
        try:
            return self._uids[int(index)]
        except KeyError:
            raise LookupError("No such element; call browser_read again.") from None

    async def click(self, index: int) -> str:
        """Click an element. If the page changed under us (a stale element id) or the element isn't ready, look at the page again,
        find the same element by its role and label, and retry once; otherwise tell the agent to use the real mouse."""
        role_label = self._labels.get(int(index))
        try:
            await self._ok("click", {"uid": self._uid(index), "includeSnapshot": False})
        except (DevToolsError, LookupError) as first:
            if not role_label:
                raise                                                       # not an element we ever showed: the plain "call browser_read again"
            await self.read()                                               # fresh element ids
            again = next((n for n, rl in self._labels.items() if rl == role_label), None) or \
                next((n for n, rl in self._labels.items() if rl[1] and rl[1] == role_label[1]), None)
            try:
                if again is None:
                    raise DevToolsError("the element is no longer on the page")
                await self._ok("click", {"uid": self._uid(again), "includeSnapshot": False})
            except (DevToolsError, LookupError) as second:
                raise DevToolsError(f"Couldn't click '{role_label[1][:50]}' as a page element ({str(second)[:120]}). "
                                    "Use the real mouse instead: screen_look, then screen_click on it.") from None
        await asyncio.sleep(0.5)
        return await self.read()

    async def type(self, index: int, text: str, submit: bool) -> str:
        await self._ok("fill", {"uid": self._uid(index), "value": text, "includeSnapshot": False})
        if submit:
            await self._ok("press_key", {"key": "Enter", "includeSnapshot": False})
            await asyncio.sleep(0.8)
        return await self.read()

    async def press(self, key: str) -> str:
        await self._need_page()
        await self._ok("press_key", {"key": key, "includeSnapshot": False})
        await asyncio.sleep(0.4)
        return await self.read()

    async def scroll(self, direction: str, amount: float) -> str:
        """Scroll the page the way a person's wheel would: whichever area of the page actually scrolls (many sites scroll an inner
        panel, not the window), and report whether it MOVED so the agent never claims a scroll that didn't happen."""
        if direction not in ("down", "up", "top", "bottom"):
            raise ValueError("direction must be down, up, top or bottom")
        await self._need_page()
        js = SCROLL_JS % {"direction": json.dumps(direction), "amount": float(amount)}
        res = await self._ok("evaluate_script", {"function": js})
        m = re.search(r"```json\s*(.*?)\s*```", res.text, re.S)
        try:
            info = json.loads(m.group(1)) if m else json.loads(res.text.strip().splitlines()[-1])
        except ValueError:
            info = {}
        info = info if isinstance(info, dict) else {}
        await asyncio.sleep(0.4)
        if not info.get("moved"):
            where = "already at the bottom" if direction in ("down", "bottom") and info.get("atEnd") else (
                "already at the top" if direction in ("up", "top") and info.get("atStart") else "didn't move")
            raise DevToolsError(f"The page {where}. If you expected it to move, the scrolling area may be elsewhere: use screen_scroll "
                                "(mouse wheel over the page) or press PageDown / space with browser_press.")
        text = await self.read()
        return f"Scrolled {direction}: now {info.get('percent', '?')}% of the way down the page.\n\n{text}"

    async def back(self) -> str:
        await self._need_page()
        await self._ok("navigate_page", {"type": "back"})
        return await self.read()

    async def forward(self) -> str:
        await self._need_page()
        await self._ok("navigate_page", {"type": "forward"})
        return await self.read()

    async def media(self, action: str) -> str:
        await self._need_page()
        res = await self._ok("evaluate_script", {"function": f"() => {media_js(action)}"})
        m = re.search(r"```json\s*(.*?)\s*```", res.text, re.S)
        value = json.loads(m.group(1)) if m else res.text
        return value if isinstance(value, str) else json.dumps(value)

    async def screenshot(self) -> tuple[bytes, str] | None:
        await self._need_page()
        res = await self._ok("take_screenshot", {"format": "jpeg", "quality": 70})
        return res.images[0] if res.images else None

    # ---- tabs: what a person does with the tab strip, done reliably instead of by clicking pixels ----
    @staticmethod
    def parse_pages(text: str) -> list[dict]:
        """"## Pages\n1: Title (https://x) [selected]" -> [{id, title, url, selected}]"""
        out = []
        for line in text.splitlines():
            m = re.match(r"^\s*(\d+):\s*(.*?)\s*$", line)
            if not m:
                continue
            body, selected = m.group(2), "[selected]" in m.group(2)
            body = body.replace("[selected]", "").strip()
            u = re.search(r"\((\w+://[^)\s]*|about:[^)\s]*)\)\s*$", body)
            out.append({"id": int(m.group(1)), "title": (body[:u.start()] if u else body).strip(), "url": u.group(1) if u else "", "selected": selected})
        return out

    async def tabs(self) -> str:
        pages = self.parse_pages((await self.client.call("list_pages")).text)
        if not pages:
            return "No tabs are open."
        lines = [f"{i}. {p['title'][:70] or '(untitled)'} - {re.sub(r'^\w+://(www\.)?', '', p['url']).split('/')[0]}" + ("  <- current" if p["id"] == self.page_id else "")
                 for i, p in enumerate(pages, 1)]
        return "Open tabs (private to the user: do not read them out unless asked):\n" + "\n".join(lines)

    async def _find_tab(self, query: str) -> dict:
        pages = self.parse_pages((await self.client.call("list_pages")).text)
        q = str(query).strip().lower()
        if q.isdigit() and 1 <= int(q) <= len(pages):
            return pages[int(q) - 1]
        words = [w for w in re.findall(r"\w+", q) if w not in {"tab", "the", "my", "a", "to", "switch"}]
        scored = sorted(((sum(w in (p["title"] + " " + p["url"]).lower() for w in words), p) for p in pages), key=lambda x: -x[0])
        if not scored or not words or scored[0][0] == 0:
            raise DevToolsError(f"No open tab matches '{query}'. Call browser_tabs to see them.")
        if len(scored) > 1 and scored[1][0] == scored[0][0] and scored[0][0] < len(words):
            raise DevToolsError(f"More than one tab matches '{query}'. Call browser_tabs and use the tab's number.")
        return scored[0][1]

    async def switch_tab(self, query: str) -> str:
        page = await self._find_tab(query)
        res = await self.client.call("select_page", {"pageId": page["id"], "bringToFront": True})
        if res.is_error:
            raise DevToolsError(strip_page_list(res.text)[:200] or "Couldn't switch tabs")
        self.page_id, self._shown = page["id"], True
        return await self.read()

    async def new_tab(self, url: str) -> str:
        await self._open(url if "://" in url or url.startswith("about:") else f"https://{url}")
        await self._ok("select_page", {"bringToFront": True})
        self._shown = True
        return await self.read()

    async def close_tab(self, query: str = "") -> str:
        page = await self._find_tab(query) if query else None
        pid = page["id"] if page else self.page_id
        if pid is None:
            raise DevToolsError("There is no tab to close.")
        res = await self.client.call("close_page", {"pageId": pid})
        if res.is_error:
            raise DevToolsError(strip_page_list(res.text)[:200] or "Couldn't close that tab")
        if pid == self.page_id:
            self.page_id = None
        return "Closed the tab."

    # ---- setup helper (used by the chrome_setup tool) ----
    async def enable_apple_events(self) -> str:
        """Open Chrome's remote-debugging page for the user. (Named for the tool contract; nothing to do with Apple Events here.)"""
        subprocess.run(["open", "-a", "Google Chrome", "chrome://inspect/#remote-debugging"], check=False)
        raise DevToolsError("I've opened Chrome's remote-debugging page. The user needs to switch on Remote Debugging there (one time) "
                            "and click Allow if Chrome asks, then tell you it's done. Say that in one plain sentence and wait; "
                            "do not call chrome_setup again until they say it's done.")

    async def close(self) -> None:
        await self.client.close()
