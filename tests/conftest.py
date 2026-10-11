import pytest

from voicectl.api import validate_event
from voicectl.core.events import EventBus


@pytest.fixture(autouse=True)
def every_published_event_honours_the_contract(monkeypatch):
    """Whatever a test makes the daemon publish must match voicectl/api/events.py: the front end relies on it."""
    bad: list[str] = []
    real = EventBus.publish

    def checked(self, type, **data):
        if type != "ui_command":                       # widget -> daemon; checked by the command protocol instead
            problem = validate_event(type, data)
            if problem:
                bad.append(problem)
        return real(self, type, **data)

    monkeypatch.setattr(EventBus, "publish", checked)
    yield
    assert not bad, f"events that break the front-end contract: {sorted(set(bad))}"
