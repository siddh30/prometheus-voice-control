"""The swarm follows the same conventions as the rest of the daemon. These fail if it ever stops."""
import pathlib
import re

SRC = pathlib.Path("src/voicectl")
SWARM = SRC / "swarm"


def files(root=SRC):
    return [p for p in root.rglob("*.py")]


def test_there_is_one_shared_memory_and_nobody_builds_their_own():
    offenders = [str(p) for p in files() if p.name != "memory.py" and re.search(r"(?<![A-Za-z_.])Memory\(", p.read_text())]
    assert not offenders, f"use core.memory.shared_memory instead: {offenders}"


def test_the_swarm_protocol_uses_the_one_schema_checker_not_its_own():
    text = (SWARM / "protocol.py").read_text()
    assert "from voicectl.api.schema import" in text and "isinstance(" not in text


def test_websockets_is_imported_only_by_the_three_transports():
    allowed = {SRC / "ui_bridge" / "server.py", SWARM / "link.py", SWARM / "server.py"}
    offenders = [str(p) for p in files() if p not in allowed and re.search(r"^\s*(from|import) websockets\b", p.read_text(), re.M)]
    assert not offenders, offenders


def test_the_swarm_is_independent_of_the_app_layers_so_a_headless_worker_can_run_it():
    forbidden = re.compile(r"^\s*(from|import) voicectl\.(modes|channels|ui_bridge|main|input|tts)\b", re.M)
    offenders = [str(p) for p in files(SWARM) if forbidden.search(p.read_text())]
    assert not offenders, f"the swarm package must not depend on: {offenders}"


def test_only_the_executor_touches_the_agent():
    offenders = [str(p) for p in files(SWARM) if p.name != "executor.py" and re.search(r"^\s*(from|import) voicectl\.agents\b", p.read_text(), re.M)]
    assert not offenders, offenders


def test_the_swarm_mode_reaches_the_swarm_only_through_the_shared_controller():
    text = (SRC / "modes" / "swarm.py").read_text()
    assert "from voicectl.swarm" not in text and 'ctx.shared.get("swarm_controller")' in text


def test_every_swarm_command_and_event_the_widget_can_trigger_is_in_the_front_end_contract():
    from voicectl.api import COMMANDS, EVENTS
    assert "swarm_pick" in COMMANDS and "swarm" in EVENTS


def test_each_app_widget_connects_to_its_own_daemons_port():
    """The Swift side hard-codes the two ports; they must be the ones the daemon configs use, or the swarm widget talks to Voice Control."""
    import yaml
    swift = pathlib.Path("overlay/Sources/VoiceOverlay/Daemon.swift").read_text()
    stable = yaml.safe_load(pathlib.Path("config.yaml").read_text())["ui"]["ws_port"]
    swarm = yaml.safe_load(pathlib.Path("config.swarm.yaml").read_text())["ui"]["ws_port"]
    assert f"ws://127.0.0.1:{swarm}" in swift and f"ws://127.0.0.1:{stable}" in swift and swarm != stable


def test_the_two_apps_refuse_to_run_together_because_they_share_the_keys():
    swift = pathlib.Path("overlay/Sources/VoiceOverlay/main.swift").read_text()
    assert "com.siddharth.voicecontrol" in swift and "com.siddharth.prometheusswarm" in swift and "is already open" in swift
