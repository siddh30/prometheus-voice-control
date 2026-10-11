"""The daemon's public API: the typed messages a front end can send (commands) and receive (events), and the
one dispatcher that routes commands to handlers. No dependency on modes, agents or the UI: copy this package
into any codebase (e.g. the swarm app) and register your own handlers."""
from voicectl.api.control import Control
from voicectl.api.protocol import COMMANDS, Command, parse_command

__all__ = ["COMMANDS", "Command", "Control", "parse_command"]
