from __future__ import annotations

"""Browser tools for the agent, served in-process (no Node/npx needed).

Two interchangeable backends behind the same interface:
  * ChromeBrowser (agents/chrome.py): drives your REAL Google Chrome through its AppleScript support. Your logins are
    already there and no extra browser/window is opened. This is the default.
  * PlaywrightBrowser: a separate automated Chromium with its own saved profile (fallback / headless-style use).

The agent reads a page as text plus a numbered list of interactive elements and acts by number, which keeps working
when sites change their markup.
"""

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import create_sdk_mcp_server, tool

from voicectl.core.config import DATA_DIR, Config

MAX_TEXT = 3500
log = logging.getLogger("voicectl.browser")

# Injected into the page. Numbers every visible interactive element (data-vc="N") and returns everything as JSON.
READ_JS = r"""
(() => {
  document.querySelectorAll('[data-vc]').forEach(e => e.removeAttribute('data-vc'));
  const sel = 'a[href],button,input,textarea,select,[role=button],[role=link],[role=tab],[contenteditable=true]';
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width < 4 || r.height < 4 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > innerHeight * 3) continue;
    n++; el.setAttribute('data-vc', n);
    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    out.push({n, tag: el.tagName.toLowerCase(), type: el.type || '', label, href: el.tagName === 'A' ? el.href.slice(0, 90) : ''});
    if (n >= 80) break;
  }
  return JSON.stringify({url: location.href, title: document.title,
                         text: (document.body ? document.body.innerText : '').slice(0, %d), elements: out});
})()
""" % MAX_TEXT


def click_js(index: int) -> str:
    return """(() => { const el = document.querySelector('[data-vc="%d"]'); if (!el) return 'MISSING';
      el.scrollIntoView({block: 'center'});
      if (el.tagName === 'A' && el.target === '_blank') el.target = '_self';   // never open extra tabs/windows
      el.click(); return 'OK'; })()""" % index


def type_js(index: int, text: str, submit: bool) -> str:
    return """(() => {
      const el = document.querySelector('[data-vc="%d"]'); if (!el) return 'MISSING';
      el.scrollIntoView({block: 'center'}); el.focus();
      const text = %s;
      if (el.isContentEditable) { document.execCommand('selectAll', false); document.execCommand('insertText', false, text); }
      else {
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);       // works with React/Vue controlled inputs
        el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
      }
      if (%s) {
        const key = t => new KeyboardEvent(t, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true});
        const handled = !el.dispatchEvent(key('keydown'));                         // a handler called preventDefault
        el.dispatchEvent(key('keypress')); el.dispatchEvent(key('keyup'));
        if (!handled && el.form && el.form.requestSubmit) setTimeout(() => { try { el.form.requestSubmit(); } catch (e) {} }, 120);
      }
      return 'OK'; })()""" % (index, json.dumps(text), "true" if submit else "false")


def scroll_js(direction: str, amount: float) -> str:
    return {"down": f"window.scrollBy(0, innerHeight * 0.85 * {amount}); 'OK'",
            "up": f"window.scrollBy(0, -innerHeight * 0.85 * {amount}); 'OK'",
            "top": "window.scrollTo(0, 0); 'OK'",
            "bottom": "window.scrollTo(0, document.body.scrollHeight); 'OK'"}.get(direction, "'BAD_DIRECTION'")


def press_js(key: str) -> str:
    return """(() => { const k = %s; const el = document.activeElement || document.body;
      if (k === 'PageDown') { window.scrollBy(0, innerHeight * 0.85); return 'OK'; }
      if (k === 'PageUp') { window.scrollBy(0, -innerHeight * 0.85); return 'OK'; }
      const codes = {Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40, ArrowUp: 38, ArrowLeft: 37, ArrowRight: 39, Backspace: 8, ' ': 32};
      for (const t of ['keydown', 'keypress', 'keyup'])
        el.dispatchEvent(new KeyboardEvent(t, {key: k, code: k, keyCode: codes[k] || 0, which: codes[k] || 0, bubbles: true, cancelable: true}));
      return 'OK'; })()""" % json.dumps(key)


def media_js(action: str) -> str:
    """Control the page's main <video>/<audio>: play, pause, toggle, mute, unmute, forward, back, restart."""
    return """(async () => {
      const v = document.querySelector('video') || document.querySelector('audio'); if (!v) return 'NO_MEDIA';
      const a = %s;
      try {
        if (a === 'play') { await v.play(); }
        else if (a === 'pause') v.pause();
        else if (a === 'toggle') { v.paused ? await v.play() : v.pause(); }
        else if (a === 'mute') v.muted = true;
        else if (a === 'unmute') v.muted = false;
        else if (a === 'forward') v.currentTime = Math.min(v.duration || 1e9, v.currentTime + 10);
        else if (a === 'back') v.currentTime = Math.max(0, v.currentTime - 10);
        else if (a === 'restart') { v.currentTime = 0; await v.play(); }
        else if (a === 'status') { /* read-only */ }
        else return 'BAD_ACTION';
      } catch (e) {
        const btn = document.querySelector('.ytp-play-button, [aria-label*="Play" i], button[title*="Play" i]');
        if (btn) btn.click(); else return 'BLOCKED: ' + e.message;
      }
      return JSON.stringify({playing: !v.paused, muted: v.muted, at: Math.round(v.currentTime), of: Math.round(v.duration || 0)});
    })()""" % json.dumps(action)


def format_page(raw: str) -> str:
    d = json.loads(raw)
    lines = [f"[{e['n']}] {e['tag']}{'(' + e['type'] + ')' if e['type'] else ''} {e['label']}"
             + (f" -> {e['href']}" if e['href'] else "") for e in d["elements"]]
    return f"URL: {d['url']}\nTITLE: {d['title']}\n\nTEXT:\n{d['text']}\n\nELEMENTS:\n" + "\n".join(lines)


class BrowserBackend(Protocol):
    last_url: str
    last_title: str
    async def goto(self, url: str) -> str: ...
    async def read(self) -> str: ...
    async def click(self, index: int) -> str: ...
    async def type(self, index: int, text: str, submit: bool) -> str: ...
    async def press(self, key: str) -> str: ...
    async def scroll(self, direction: str, amount: float) -> str: ...
    async def back(self) -> str: ...
    async def forward(self) -> str: ...
    async def media(self, action: str) -> str: ...
    def has_page(self) -> bool: ...
    async def screenshot(self) -> tuple[bytes, str] | None: ...
    async def close(self) -> None: ...


class PlaywrightBrowser:
    """A separate automated Chromium with its own saved profile (logins persist after you sign in once)."""

    def __init__(self, profile: Path, headless: bool = False) -> None:
        self.profile = profile
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._page = None
        self.last_url = ""
        self.last_title = ""

    async def page(self):
        if self._page is None or self._page.is_closed():
            if self._ctx is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                self.profile.mkdir(parents=True, exist_ok=True)
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    str(self.profile), headless=self.headless, viewport={"width": 1280, "height": 860})
            self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        return self._page

    async def read(self) -> str:
        page = await self.page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        raw = await page.evaluate(READ_JS)
        d = json.loads(raw)
        self.last_url, self.last_title = d["url"], d["title"]
        return format_page(raw)

    async def goto(self, url: str) -> str:
        page = await self.page()
        await page.goto(url if "://" in url else f"https://{url}", wait_until="domcontentloaded", timeout=30000)
        return await self.read()

    async def click(self, index: int) -> str:
        page = await self.page()
        el = await page.query_selector(f'[data-vc="{index}"]')
        if not el:
            raise LookupError("No such element; call browser_read again.")
        await el.click(timeout=8000)
        await page.wait_for_timeout(800)
        return await self.read()

    async def type(self, index: int, text: str, submit: bool) -> str:
        page = await self.page()
        el = await page.query_selector(f'[data-vc="{index}"]')
        if not el:
            raise LookupError("No such element; call browser_read again.")
        await el.fill(text)
        if submit:
            await el.press("Enter")
            await page.wait_for_timeout(1000)
        return await self.read()

    async def press(self, key: str) -> str:
        page = await self.page()
        await page.keyboard.press(key)
        await page.wait_for_timeout(500)
        return await self.read()

    async def scroll(self, direction: str, amount: float) -> str:
        page = await self.page()
        if (r := await page.evaluate(scroll_js(direction, amount))) != "OK":
            raise ValueError("direction must be down, up, top or bottom")
        await page.wait_for_timeout(500)
        return await self.read()

    async def back(self) -> str:
        await (await self.page()).go_back(wait_until="domcontentloaded", timeout=15000)
        return await self.read()

    async def forward(self) -> str:
        await (await self.page()).go_forward(wait_until="domcontentloaded", timeout=15000)
        return await self.read()

    def has_page(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def media(self, action: str) -> str:
        return await (await self.page()).evaluate(media_js(action))

    async def screenshot(self) -> tuple[bytes, str] | None:
        return await (await self.page()).screenshot(type="png"), "image/png"

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()


Browser = PlaywrightBrowser          # backwards-compatible name


def _text(s: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if error else {})}


def build_browser_server(browser: BrowserBackend):
    """The browser_* tools the agent sees. Errors come back as tool errors (with advice), never as crashes."""

    async def safe(coro) -> dict[str, Any]:
        try:
            return _text(await coro)
        except Exception as e:
            log.warning("browser tool failed: %s", e)
            return _text(f"{e}", error=True)

    @tool("browser_goto", "Navigate the browser to a URL and return the page text and numbered elements.", {"url": str})
    async def goto(args): return await safe(browser.goto(args["url"]))

    @tool("browser_read", "Read the current page: text plus numbered clickable/typeable elements.", {})
    async def read(args): return await safe(browser.read())

    @tool("browser_click", "Click an element by its number from the last browser_read/goto. "
                           "Also pass `description` (what you are clicking, in words).", {"index": int, "description": str})
    async def click(args): return await safe(browser.click(int(args["index"])))

    @tool("browser_type", "Type text into an input by its number. Set submit=true to press Enter afterwards.",
          {"index": int, "text": str, "submit": bool, "description": str})
    async def type_(args): return await safe(browser.type(int(args["index"]), args["text"], bool(args.get("submit"))))

    @tool("browser_press", "Press a keyboard key in the page (e.g. Enter, Escape, PageDown).", {"key": str})
    async def press(args): return await safe(browser.press(args["key"]))

    @tool("browser_scroll", "Scroll the page. direction: 'down', 'up', 'top' or 'bottom'. amount: screens (default 1).",
          {"direction": str, "amount": float})
    async def scroll(args): return await safe(browser.scroll(args.get("direction", "down"), float(args.get("amount") or 1)))

    @tool("browser_back", "Go back to the previous page in the browser history.", {})
    async def back(args): return await safe(browser.back())

    @tool("browser_forward", "Go forward in the browser history.", {})
    async def forward(args): return await safe(browser.forward())

    @tool("browser_media", "Control the video/audio playing on the page. action: play, pause, toggle, mute, unmute, "
                           "forward (10s), back (10s), restart. Returns the playback state.", {"action": str})
    async def media(args):
        try:
            r = await browser.media(args["action"])
        except Exception as e:
            return _text(f"{e}", error=True)
        if r in ("NO_MEDIA", "BAD_ACTION") or r.startswith("BLOCKED"):
            return _text({"NO_MEDIA": "There is no video or audio on this page.",
                          "BAD_ACTION": "action must be play, pause, toggle, mute, unmute, forward, back or restart."}.get(r, r), error=True)
        return _text(r)

    def tab_tool(name: str, desc: str, schema: dict, method: str, arg: str | None):
        @tool(name, desc, schema)
        async def run(args):
            fn = getattr(browser, method, None)
            if fn is None:
                return _text("Tab control needs the Chrome DevTools connection; use keyboard shortcuts with screen_key instead "
                             "(cmd+1..9, ctrl+tab, cmd+t, cmd+w).", error=True)
            return await safe(fn(args.get(arg, "")) if arg else fn())
        return run

    tabs = tab_tool("browser_tabs", "List the open Chrome tabs (numbered). Use before switching to a tab.", {}, "tabs", None)
    switch = tab_tool("browser_switch_tab", "Switch to an already open tab and bring it to the front, like clicking it. "
                      "query: words from its title or site (e.g. 'linkedin profile') or its number from browser_tabs.", {"query": str}, "switch_tab", "query")
    new_tab = tab_tool("browser_new_tab", "Open a URL in a NEW tab (like cmd+T then typing the address) and switch to it.", {"url": str}, "new_tab", "url")
    close_tab = tab_tool("browser_close_tab", "Close a tab (like cmd+W). query: words from its title or its number; empty = the current tab.",
                         {"query": str}, "close_tab", "query")

    @tool("browser_screenshot", "Take a screenshot of the visible page to see layout, images or charts (not always available).", {})
    async def screenshot(args):
        import base64
        try:
            shot = await browser.screenshot()
        except Exception as e:
            return _text(f"{e}", error=True)
        if shot is None:
            return _text("Screenshots aren't available here; use browser_read to see the page.", error=True)
        data, mime = shot
        return {"content": [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}]}

    return create_sdk_mcp_server("browser", tools=[goto, read, click, type_, press, scroll, back, forward, media, tabs, switch, new_tab, close_tab, screenshot])


def default_browser(config: Config | None = None, screen=None) -> BrowserBackend:
    """browser.backend: auto (default) | devtools | chrome | playwright.
    devtools = Google's chrome-devtools-mcp attached to your own Chrome (real clicks, built-in screenshots);
    chrome   = your Chrome via AppleScript + page JavaScript; playwright = a separate automated Chromium."""
    backend = config.get("browser.backend", "auto") if config else "auto"
    if backend in ("auto", "devtools"):
        from voicectl.agents import devtools
        if devtools.command():
            return devtools.DevToolsBrowser()
        if backend == "devtools":
            log.warning("browser.backend is 'devtools' but Node / chrome-devtools-mcp isn't installed; falling back to AppleScript")
    if backend in ("auto", "devtools", "chrome"):
        from voicectl.agents.chrome import ChromeBrowser
        return ChromeBrowser(config.get("browser.app", "Google Chrome") if config else "Google Chrome", screen)
    return PlaywrightBrowser(DATA_DIR / "browser_profile", bool(config.get("browser.headless", False)) if config else False)
