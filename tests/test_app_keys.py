from voicectl.input.app_keys import handle_key


def run(msg):
    calls = []
    handle_key(msg, {"f9": "speak", "f11": "agent"}, "esc",
               lambda m: calls.append(("down", m)), lambda m: calls.append(("up", m)), lambda: calls.append(("cancel",)))
    return calls


def test_mode_keys():
    assert run({"name": "f9", "down": True}) == [("down", "speak")]
    assert run({"name": "f11", "down": False}) == [("up", "agent")]


def test_cancel_only_on_down():
    assert run({"name": "esc", "down": True}) == [("cancel",)]
    assert run({"name": "esc", "down": False}) == []


def test_unknown_key_ignored():
    assert run({"name": "f5", "down": True}) == []
