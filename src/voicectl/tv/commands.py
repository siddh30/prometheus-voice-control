"""TV control: turn one spoken sentence into one remote-control command from a fixed list, or into nothing.

This is the whole of what the TV mode can ask the TV to do. There is no free-form command, no shell and no
setting: a sentence that matches none of the patterns below is refused, never guessed at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# What people say -> Android package (the TV launches an app by package). `tv.apps` in config.yaml adds to this.
DEFAULT_APPS: dict[str, str] = {
    "netflix": "com.netflix.ninja",
    "youtube": "com.google.android.youtube.tv",
    "you tube": "com.google.android.youtube.tv",
    "prime video": "com.amazon.amazonvideo.livingroom",
    "prime": "com.amazon.amazonvideo.livingroom",
    "amazon prime": "com.amazon.amazonvideo.livingroom",
    "disney plus": "com.disney.disneyplus",
    "disney": "com.disney.disneyplus",
    "hulu": "com.hulu.livingroomplus",
    "peacock": "com.peacocktv.peacockandroid",
    "paramount plus": "com.cbs.ott",
    "paramount": "com.cbs.ott",
    "apple tv": "com.apple.atve.androidtv.appletv",
    "max": "com.wbd.stream",
    "tubi": "com.tubitv",
    "espn": "com.espn.score_center",
}

_TV = r"(?:the )?(?:tv|television)"
_SCREEN = r"(?:my |the |this )?(?:mac(?: mini)?|screen|display|desktop|computer)"
EXTEND_PHRASE = re.compile(
    rf"extend {_SCREEN}(?: (?:to|onto|on) {_TV})?"
    rf"|extend (?:to|onto|on) {_TV}")
MIRROR_PHRASE = re.compile(                              # "bring my work in" mirrors the main screen: whatever is on it (Citrix too) shows on the TV
    rf"mirror {_SCREEN}(?: (?:to|onto|on) {_TV})?"
    rf"|mirror (?:to|onto|on) {_TV}"
    rf"|bring (?:my |the )?(?:work|screen|desktop|display|mac(?: mini)?|computer) (?:in|over|here)"
    rf"|bring (?:my |the )?(?:work|screen|desktop|display|mac(?: mini)?|computer) (?:to|onto|on) {_TV}"
    rf"|(?:put|show) (?:my )?(?:work|screen|desktop) on {_TV}")
STOP_PHRASE = re.compile(
    rf"(?:switch|go|get|change|come) back (?:to )?(?:{_TV}|normal tv|normal)"
    rf"|back to {_TV}"
    rf"|(?:stop|end) extending(?: {_SCREEN})?(?: (?:to|onto|on) {_TV})?"
    rf"|disconnect {_TV}|disconnect (?:the )?(?:display|screen)")

_NUMBER_WORDS = {w: i for i, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"])}
_DEFAULT_VOLUME_STEPS = 2


@dataclass(frozen=True)
class TvCommand:
    kind: str              # "key" | "power" | "launch" | "link" | "volume_to" | "wait" | "extend_display" | "mirror_display" | "stop_extending"
    key: str = ""          # a RemoteKeyCode name without the KEYCODE_ prefix ("VOLUME_UP", "HOME")
    package: str = ""      # for "launch": the app's package; for "link": the web link the TV opens
    repeat: int = 1        # how many times to press `key`; for "volume_to" the level (0 to 100); for "wait" the seconds
    want: str = ""         # for "power": "on" or "off"
    label: str = ""        # short sentence that is shown and spoken


def _clean(text: str) -> str:
    t = re.sub(r"[^\w\s]", " ", text.lower())
    t = re.sub(r"\b(please|hey|prometheus|can you|could you|would you|for me)\b", " ", t)
    t = re.sub(r"\b(on|to|at) (the )?(tv|television)\b", " ", t)          # "open netflix on the tv"
    t = re.sub(r"\b(the )?(tv|television)( please)?$", lambda m: m.group(0) if t.strip() in ("tv", "television") else " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _count(raw: str | None, cap: int) -> int:
    if not raw:
        return min(_DEFAULT_VOLUME_STEPS, cap)
    n = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, _DEFAULT_VOLUME_STEPS)
    return max(1, min(n, cap))


def parse(text: str, apps: dict[str, str] | None = None, inputs: dict[str, int] | None = None,
          max_steps: int = 10) -> TvCommand | None:
    """The command for `text`, or None when it is not on the allow-list."""
    raw = re.sub(r"\s+", " ", re.sub(r"\b(please|hey|prometheus|can you|could you|would you|for me)\b", " ",
                                      re.sub(r"[^\w\s]", " ", text.lower()))).strip()
    if EXTEND_PHRASE.fullmatch(raw):                       # checked on the words as spoken: _clean drops "to the TV"
        return TvCommand("extend_display", label="Bringing your screen to the TV")
    if MIRROR_PHRASE.fullmatch(raw):
        return TvCommand("mirror_display", label="Putting your main screen on the TV")
    if STOP_PHRASE.fullmatch(raw):
        return TvCommand("stop_extending", label="Switching back to the TV")
    t = _clean(text)
    if not t:
        return None
    app_names = {**DEFAULT_APPS, **{k.lower(): v for k, v in (apps or {}).items()}}
    input_names = {k.lower(): int(v) for k, v in (inputs or {}).items()}
    num = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"

    if m := re.fullmatch(r"(?:turn|switch|power) (?:the )?(on|off)|(?:turn|switch|power) (on|off)|(?:tv|power) (on|off)", t):
        want = next(g for g in m.groups() if g)
        return TvCommand("power", "POWER", want=want, label=f"Turning the TV {want}")

    if m := re.fullmatch(rf"(?:turn )?(?:the )?volume (up|down)(?: (?:by )?{num})?|turn it (up|down)(?: {num})?", t):
        g = m.groups()
        direction = g[0] or g[2]
        steps = _count(g[1] or g[3], max_steps)
        return TvCommand("key", f"VOLUME_{direction.upper()}", repeat=steps, label=f"Volume {direction}")
    if m := re.fullmatch(rf"(louder|quieter|softer|lower|turn it up|turn it down)(?: {num})?", t):
        up = m.group(1) in ("louder", "turn it up")
        return TvCommand("key", "VOLUME_UP" if up else "VOLUME_DOWN", repeat=_count(m.group(2), max_steps),
                         label="Volume up" if up else "Volume down")
    if m := re.fullmatch(r"(?:set |turn |put |make )?(?:the )?volume (?:to |at )?(\d{1,3})(?: percent)?", t):
        level = min(int(m.group(1)), 100)
        return TvCommand("volume_to", repeat=level, label=f"Volume {level}")
    if re.fullmatch(r"(mute|unmute)(?: (?:the )?(?:sound|volume))?", t):
        return TvCommand("key", "MUTE", label="Toggled mute")

    if m := re.fullmatch(r"(?:switch|go|change|put it) (?:to )?(?:the )?(?:input )?hdmi ?(\d)", t):
        port = int(m.group(1))
        return TvCommand("key", f"TV_INPUT_HDMI_{port}", label=f"Switching to HDMI {port}") if 1 <= port <= 4 else None
    if m := re.fullmatch(r"(?:switch|go|change|put it) (?:to )?(?:the )?(.+)", t):
        name = m.group(1)
        if name in input_names and 1 <= input_names[name] <= 4:
            return TvCommand("key", f"TV_INPUT_HDMI_{input_names[name]}", label=f"Switching to {name}")
    if re.fullmatch(r"(?:(?:show|open|switch) )?(?:the )?inputs?|(?:change|switch) input", t):
        return TvCommand("key", "TV_INPUT", label="Opening inputs")

    if re.fullmatch(r"(?:go )?(?:to )?(?:the )?home(?: screen)?|go home", t):
        return TvCommand("key", "HOME", label="Going home")
    if re.fullmatch(r"(?:go )?back", t):
        return TvCommand("key", "BACK", label="Going back")
    if re.fullmatch(r"(?:press )?(?:select|ok|okay|enter|confirm)", t):
        return TvCommand("key", "DPAD_CENTER", label="Select")
    if m := re.fullmatch(r"(?:go|move|press|scroll) (up|down|left|right)", t):
        return TvCommand("key", f"DPAD_{m.group(1).upper()}", label=f"Moving {m.group(1)}")
    if re.fullmatch(r"(?:play|pause|resume|stop)(?: (?:it|the video|playback|the show|the movie))?", t):
        return TvCommand("key", "MEDIA_PLAY_PAUSE", label="Play or pause")

    verb = r"(?:(?:open|launch|start|play|put on|go to|watch|show me|switch to) )?"
    for spoken in sorted(app_names, key=len, reverse=True):
        if re.fullmatch(verb + re.escape(spoken), t):
            return TvCommand("launch", package=app_names[spoken], label=f"Opening {spoken.title()}")
    return None


APP_LOAD_WAIT_S = 6          # after opening an app, how long a following command waits for it to come up
MAX_CHAIN = 5


def parse_many(text: str, apps: dict[str, str] | None = None, inputs: dict[str, int] | None = None,
               max_steps: int = 10) -> list[TvCommand] | None:
    """One command, or several joined by "and", "then" or a comma ("open YouTube and turn the volume down").
    None unless every part is on the allow-list."""
    one = parse(text, apps, inputs, max_steps)
    if one:
        return [one]
    parts = [p for p in re.split(r"\s*(?:,|\band then\b|\bthen\b|\band\b)\s*", text.lower()) if p.strip()]
    if not 2 <= len(parts) <= MAX_CHAIN:
        return None
    out: list[TvCommand] = []
    for part in parts:
        cmd = parse(part, apps, inputs, max_steps)
        if cmd is None:
            return None
        if out and out[-1].kind in ("launch", "link"):
            out.append(TvCommand("wait", repeat=APP_LOAD_WAIT_S))
        out.append(cmd)
    if any(c.kind in ("extend_display", "mirror_display", "stop_extending") for c in out):
        return None                     # a screen step changes the Mac's displays, so it is always said on its own
    return out
