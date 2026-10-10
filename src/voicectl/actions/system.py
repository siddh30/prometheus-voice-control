from voicectl.actions._sys import osascript, sh
from voicectl.actions.base import Action, ActionResult, actions


@actions.register("sleep_mac")
class SleepMac(Action):
    name = "sleep_mac"
    description = "Put the computer to sleep."
    patterns = [r"(?:put (?:the |my )?(?:computer|mac|machine) to )?sleep(?: (?:the |my )?(?:computer|mac))?", r"go to sleep"]

    def run(self) -> ActionResult:
        r = sh(["pmset", "sleepnow"])
        return ActionResult(r.ok, "Sleeping" if r.ok else r.message)


@actions.register("lock_screen")
class LockScreen(Action):
    name = "lock_screen"
    description = "Lock the screen."
    patterns = [r"lock(?: the| my)?(?: screen| computer| mac)?"]

    def run(self) -> ActionResult:
        r = sh(["pmset", "displaysleepnow"])
        return ActionResult(r.ok, "Locked" if r.ok else r.message)


_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                      "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_NUM = (r"\d{1,3}|(?:a |one )?hundred|(?:" + "|".join(_TENS) + r")(?:[ -](?:" + "|".join(list(_ONES)[1:10]) + r"))?|" + "|".join(_ONES))


def parse_number(text: str) -> int | None:
    """"thirty five" / "forty-two" / "a hundred" / "30" -> int (0-100), else None."""
    t = text.lower().strip().replace("-", " ")
    if t.isdigit():
        return int(t)
    if t in ("hundred", "a hundred", "one hundred"):
        return 100
    parts = t.split()
    if len(parts) == 1:
        return _ONES.get(parts[0], _TENS.get(parts[0]))
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES and 1 <= _ONES[parts[1]] <= 9:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return None


@actions.register("set_volume")
class SetVolume(Action):
    name = "set_volume"
    description = "Set the output volume to a percentage from 0 to 100."
    patterns = [r"(?:set |turn )?(?:the )?(?:volume|sound)(?: up| down)?(?: to| at)? (?P<level>" + _NUM + r")(?: percent|%)?"]
    params = {"level": "Volume 0-100"}

    def run(self, level: str) -> ActionResult:
        value = parse_number(level)
        if value is None:
            return ActionResult(False, f"I didn't understand the volume '{level}'", "I didn't catch the volume level.")
        n = max(0, min(100, value))
        r = osascript(f"set volume output volume {n}")
        return ActionResult(r.ok, f"Volume {n}%" if r.ok else r.message)


@actions.register("mute")
class Mute(Action):
    name = "mute"
    description = "Mute the output volume."
    patterns = [r"mute(?: the)?(?: volume| sound| audio)?"]

    def run(self) -> ActionResult:
        r = osascript("set volume output muted true")
        return ActionResult(r.ok, "Muted" if r.ok else r.message)
