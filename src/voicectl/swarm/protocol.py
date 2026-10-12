from __future__ import annotations

from typing import Any

from voicectl.api.schema import Table, check

PROTOCOL_VERSION = 1

# Controller -> worker. Every message is {"type": <name>, ...fields}. A field is (python type(s), required).
COMMANDS: Table = {
    "hello":    {"token": (str, True), "controller": (str, False), "version": (int, False)},   # first message on a connection
    "task":     {"id": (str, True), "text": (str, True), "context": (str, False)},
    "cancel":   {"id": (str, True)},
    "approval": {"id": (str, True), "approved": (bool, True)},                                   # answer to a worker's approval request
    "ping":     {},
}

# Worker -> controller.
EVENTS: Table = {
    "welcome":   {"name": (str, True), "version": (int, True)},
    "rejected":  {"reason": (str, True)},                                    # bad token or protocol; the connection is closed
    "heartbeat": {},                                                         # every few seconds: this worker is alive
    "started":   {"id": (str, True)},
    "checklist": {"id": (str, True), "items": (list, True)},                 # items: {text, status} with pending|in_progress|done|skipped
    "step":      {"id": (str, True), "text": (str, True), "done": (bool, False), "failed": (bool, False)},
    "approval":  {"id": (str, True), "text": (str, True)},                   # the worker needs a yes or no (money, sending, deleting)
    "result":    {"id": (str, True), "text": (str, True)},
    "done":      {"id": (str, True)},
    "failed":    {"id": (str, True), "error": (str, True)},
    "stalled":   {"id": (str, True), "seconds": ((int, float), True)},       # running, but nothing has happened for a while
    "resumed":   {"id": (str, True)},
}


def validate(table: Table, message: dict[str, Any]) -> str | None:
    """None if the message honours the table, else a one-line reason. Used on both ends and in tests."""
    return check(table, message.get("type"), message, "message type")
