"""Restart on use: when, and only when, gcx may start an endpoint over SSH."""

import subprocess

import pytest

from gcx import restart


class Client:
    def __init__(self, statuses):
        self.statuses = list(statuses)

    def get_endpoint_status(self, ep):
        return {"status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}


CFG = {"endpoint": "ep", "ssh": "delta", "keepalive": {"mode": "on-use"},
       "remote_root": "~/.gcx", "endpoint_name": "gcx"}


@pytest.fixture
def ssh(monkeypatch):
    calls = []
    monkeypatch.setattr(restart.time, "sleep", lambda s: None)

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "started-on-dt-login02\n", "")
    monkeypatch.setattr(restart.subprocess, "run", fake_run)
    return calls


def test_online_endpoint_is_left_alone(ssh):
    restart.ensure_online("delta", CFG, Client(["online"]))
    assert ssh == []


def test_other_modes_never_restart(ssh):
    restart.ensure_online("midway3", dict(CFG, keepalive={"mode": "failover"}), Client(["offline"]))
    assert ssh == []


def test_brief_offline_blip_does_not_restart(ssh):
    restart.ensure_online("delta", CFG, Client(["offline", "online"]))
    assert ssh == []


def test_offline_with_live_master_restarts_and_waits(ssh):
    restart.ensure_online("delta", CFG, Client(["offline", "offline", "offline", "online"]))
    check, start = ssh
    assert check[:3] == ["ssh", "-O", "check"]
    assert start[:4] == ["ssh", "-o", "BatchMode=yes", "delta"]
    assert "already-running-here" in start[4] and "start --detach gcx" in start[4]


def test_offline_without_master_asks_for_login(monkeypatch):
    monkeypatch.setattr(restart.time, "sleep", lambda s: None)
    monkeypatch.setattr(restart.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", ""))
    with pytest.raises(restart.EndpointOffline, match="gcx login delta"):
        restart.ensure_online("delta", CFG, Client(["offline"]))
