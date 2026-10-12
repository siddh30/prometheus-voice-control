import socket

from voicectl.swarm.__main__ import worker_name


class Config(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_the_env_key_wins(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_NAME", "macbook-air")
    assert worker_name(Config({"swarm.worker.name": "FromConfig"})) == "macbook-air"


def test_then_the_config_then_the_hostname(monkeypatch):
    monkeypatch.delenv("SWARM_WORKER_NAME", raising=False)
    assert worker_name(Config({"swarm.worker.name": "FromConfig"})) == "FromConfig"
    monkeypatch.setattr(socket, "gethostname", lambda: "Siddharths-MacBook-Air-2.local")
    assert worker_name(Config()) == "Siddharths-MacBook-Air-2"
