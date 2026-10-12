import pathlib
import re

from voicectl.api import COMMANDS, EVENTS

DESIGN = pathlib.Path("DESIGN.md").read_text()


def rows(section: str) -> set[str]:
    """The first-column names of the markdown table under a heading."""
    body = DESIGN[DESIGN.index(section):]
    body = body[:body.index("\n### ", 10)] if "\n### " in body[10:] else body
    return set(re.findall(r"^\| `([a-z_]+)` \|", body, re.M))


def test_every_command_is_documented_and_nothing_extra():
    assert rows("### Front end to daemon: commands") == set(COMMANDS)


def test_every_event_is_documented_and_nothing_extra():
    assert rows("### Daemon to front end: events") == set(EVENTS)


def test_every_swarm_message_is_documented_and_nothing_extra():
    from voicectl.swarm.protocol import COMMANDS as WC, EVENTS as WE
    assert rows_between("Controller to worker:", "Worker to controller:") == set(WC)
    assert rows_between("Worker to controller:", "### Python interfaces") == set(WE)


def rows_between(start: str, end: str) -> set[str]:
    body = DESIGN[DESIGN.index(start):]
    body = body[:body.index(end, len(start))]
    return set(re.findall(r"^\| `([a-z_]+)` \|", body, re.M))


def test_the_files_the_design_names_exist():
    named = set(re.findall(r"`((?:api|channels|llm|tts|platform|core|stt|swarm)/[a-z_]+\.py)`", DESIGN))
    named |= set(re.findall(r"`([A-Za-z]+\.swift)`", DESIGN))
    assert named
    for name in named:
        py = pathlib.Path("src/voicectl") / name
        swift = list(pathlib.Path("overlay/Sources").glob(f"*/{name}"))
        assert py.exists() or swift, f"DESIGN.md names {name}, which does not exist"


def test_every_interface_has_its_place_in_the_design():
    for needed in ("api/schema.py", "swarm/protocol.py", "swarm/link.py", "swarm/worker.py", "swarm/router.py", "swarm/controller.py", "swarm/board.py", "api/control.py", "api/requests.py", "channels/voice.py", "channels/text.py", "llm/__init__.py",
                   "tts/engines.py", "platform/__init__.py", "core/memory.py", "core/learnings.py", "stt/base.py"):
        assert needed in DESIGN, f"{needed} is not in the interface table"
    for pathway in ("Voice input (push-to-talk)", "Typing", "Commands (widget to daemon)", "Events (daemon to widget)", "Screen and mouse",
                    "Dictation (F9)", "Approvals", "Music mode", "Swarm request", "Swarm links and stalls", "Switching between the apps", "Releases and profiles"):
        assert f"### {pathway}" in DESIGN, f"no diagram for {pathway}"


def test_the_diagrams_are_well_formed():
    blocks = re.findall(r"```mermaid\n(.*?)```", DESIGN, re.S)
    assert len(blocks) >= 13
    for b in blocks:
        assert re.match(r"(flowchart|sequenceDiagram|stateDiagram-v2|graph)\b", b.strip()), b[:40]
    assert DESIGN.count("```") % 2 == 0                                  # no unclosed fence swallowing the page
