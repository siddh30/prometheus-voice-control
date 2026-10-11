from __future__ import annotations

"""Eyes and hands for the agent: look at the screen (Chrome's window by default) and drive the mouse and keyboard.

The Voice Control app does the actual capturing and clicking (it holds the Screen Recording / Accessibility permissions);
this module asks it over the UI WebSocket and maps coordinates. The model sees a downscaled screenshot and gives
coordinates in THAT image's pixels; we convert them to real screen points."""

import base64
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from voicectl.api import RequestBroker, RequestTimeout


class ScreenError(RuntimeError):
    pass


class ScreenLink:
    """The agent's line to the app that can see and touch the screen: one request, one answer, through the API broker."""

    def __init__(self, broker: RequestBroker) -> None:
        self.broker = broker

    async def request(self, op: str, timeout: float = 20.0, **args: Any) -> dict[str, Any]:
        try:
            res = await self.broker.request("input_request", timeout, op=op, **args)
        except RequestTimeout:
            raise ScreenError("The Voice Control app didn't answer. Screen control only works while the app is open.") from None
        if not res.get("ok"):
            raise ScreenError(res.get("error") or "Screen control failed.")
        return res


class Screen:
    """Screen operations in the coordinates the model works in (pixels of the last screenshot it was shown)."""

    def __init__(self, link: ScreenLink) -> None:
        self.link = link
        self.geom: dict[str, float] | None = None       # origin/size in points + the screenshot's pixel size
        self.mark: tuple[float, float] | None = None    # where the pointer last acted (points): drawn on the next screenshot

    async def look(self, target: str = "chrome") -> tuple[bytes, str, str]:
        extra = {"markX": self.mark[0], "markY": self.mark[1]} if self.mark else {}
        r = await self.link.request("screenshot", target=target, **extra)
        self.geom = {k: float(r[k]) for k in ("originX", "originY", "widthPt", "heightPt", "imgW", "imgH")}
        g = self.geom
        note = (f"Screenshot of {'the Chrome window' if target == 'chrome' else 'the screen'}, {int(g['imgW'])}x{int(g['imgH'])} px. "
                "All x,y coordinates you give are pixels in THIS image, origin at the top-left."
                + (" The red ring marks where the pointer last clicked or moved: if it isn't on your target, correct your aim." if self.mark else ""))
        return base64.b64decode(r["image"]), r.get("mime", "image/jpeg"), note

    def to_points(self, x: float, y: float) -> tuple[float, float]:
        if not self.geom:
            raise ScreenError("Take a screenshot first (screen_look) so I know where things are.")
        g = self.geom
        if not (0 <= x <= g["imgW"] and 0 <= y <= g["imgH"]):
            raise ScreenError(f"({x:.0f}, {y:.0f}) is outside the screenshot ({int(g['imgW'])}x{int(g['imgH'])}).")
        return g["originX"] + x * g["widthPt"] / g["imgW"], g["originY"] + y * g["heightPt"] / g["imgH"]

    async def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        px, py = self.to_points(x, y)
        self.mark = (px, py)
        await self.link.request("click", x=px, y=py, button=button, count=count)

    async def move(self, x: float, y: float) -> None:
        px, py = self.to_points(x, y)
        self.mark = (px, py)
        await self.link.request("move", x=px, y=py)

    async def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        (px, py), (qx, qy) = self.to_points(x, y), self.to_points(x2, y2)
        await self.link.request("drag", x=px, y=py, x2=qx, y2=qy)

    async def scroll(self, direction: str, amount: float, x: float | None = None, y: float | None = None) -> None:
        if not self.geom:
            await self.look()
        g = self.geom
        px, py = self.to_points(x if x is not None else g["imgW"] / 2, y if y is not None else g["imgH"] / 2)
        pixels = int(900 * amount)                      # about one screen per unit
        dy = {"down": -pixels, "up": pixels}.get(direction, 0)
        dx = {"right": -pixels, "left": pixels}.get(direction, 0)
        if not (dx or dy):
            raise ScreenError("direction must be up, down, left or right")
        await self.link.request("scroll", x=px, y=py, dx=dx, dy=dy)

    async def type(self, text: str) -> None:
        await self.link.request("type", text=text)

    async def key(self, key: str, mods: list[str] | None = None) -> None:
        await self.link.request("key", key=key, mods=mods or [])


def build_screen_server(screen: Screen):
    """screen_* tools. Every action returns a fresh screenshot so the model can verify what happened."""

    def img(data: bytes, mime: str) -> dict[str, Any]:
        return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}

    async def shot(prefix: str) -> dict[str, Any]:
        await asyncio.sleep(0.7)                        # let the page react before looking again
        data, mime, note = await screen.look()
        return {"content": [{"type": "text", "text": f"{prefix} {note}"}, img(data, mime)]}

    async def guarded(prefix: str, coro) -> dict[str, Any]:
        try:
            await coro
            return await shot(prefix)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"{e}"}], "is_error": True}

    @tool("screen_look", "Look at the screen. target: 'chrome' (default, the Chrome window) or 'screen' (whole display, "
                         "for other apps). Use this to see what is really there before clicking.", {"target": str})
    async def look(args):
        try:
            data, mime, note = await screen.look(args.get("target") or "chrome")
            return {"content": [{"type": "text", "text": note}, img(data, mime)]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"{e}"}], "is_error": True}

    @tool("screen_click", "Click at x,y (pixels of the latest screenshot). Say what you are clicking in `description`. "
                          "button: left or right; count: 2 for a double click.",
          {"x": float, "y": float, "description": str, "button": str, "count": int})
    async def click(args):
        return await guarded("Clicked.", screen.click(float(args["x"]), float(args["y"]),
                                                      args.get("button") or "left", int(args.get("count") or 1)))

    @tool("screen_move", "Move the mouse pointer to x,y (pixels of the latest screenshot), e.g. to hover over something.",
          {"x": float, "y": float})
    async def move(args): return await guarded("Moved the mouse.", screen.move(float(args["x"]), float(args["y"])))

    @tool("screen_nudge", "Move the mouse pointer a little in a direction ('up','down','left','right') by `pixels` "
                          "(screenshot pixels), relative to where you last pointed.", {"direction": str, "pixels": float})
    async def nudge(args):
        g = screen.geom
        if not g:
            return {"content": [{"type": "text", "text": "Take a screenshot first (screen_look)."}], "is_error": True}
        cx, cy = getattr(screen, "_cursor", (g["imgW"] / 2, g["imgH"] / 2))
        d, n = args.get("direction", "down"), float(args.get("pixels") or 100)
        cx += {"left": -n, "right": n}.get(d, 0)
        cy += {"up": -n, "down": n}.get(d, 0)
        cx, cy = min(max(cx, 0), g["imgW"]), min(max(cy, 0), g["imgH"])
        screen._cursor = (cx, cy)
        return await guarded(f"Moved the mouse {d}.", screen.move(cx, cy))

    @tool("screen_scroll", "Scroll with the mouse wheel. direction: up, down, left or right; amount: screens (default 1). "
                           "Optionally x,y to scroll a specific area.", {"direction": str, "amount": float, "x": float, "y": float})
    async def scroll(args):
        x, y = args.get("x"), args.get("y")
        return await guarded(f"Scrolled {args.get('direction', 'down')}.", screen.scroll(
            args.get("direction", "down"), float(args.get("amount") or 1),
            float(x) if x not in (None, "") else None, float(y) if y not in (None, "") else None))

    @tool("screen_drag", "Press at x,y, drag to x2,y2 and release (sliders, selecting text, moving things).",
          {"x": float, "y": float, "x2": float, "y2": float, "description": str})
    async def drag(args):
        return await guarded("Dragged.", screen.drag(float(args["x"]), float(args["y"]), float(args["x2"]), float(args["y2"])))

    @tool("screen_type", "Type text into whatever has keyboard focus (click the field first). Say what you are typing "
                         "into in `description`.", {"text": str, "description": str})
    async def type_(args): return await guarded("Typed.", screen.type(args["text"]))

    @tool("screen_key", "Press a key, optionally with modifiers: key like return, tab, escape, space, delete, up, down, "
                        "left, right, pageup, pagedown, home, end, or a letter; mods from cmd, shift, alt, ctrl.",
          {"key": str, "mods": list, "description": str})
    async def key(args): return await guarded(f"Pressed {args['key']}.", screen.key(args["key"], list(args.get("mods") or [])))

    return create_sdk_mcp_server("screen", tools=[look, click, move, nudge, scroll, drag, type_, key])
