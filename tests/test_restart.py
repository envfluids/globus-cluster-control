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


class StoppableClient:
    """online until stop_endpoint is called, then offline until started."""
    def __init__(self, online=True, lock_in=0.0):
        self.online, self.lock_in, self.stops = online, lock_in, 0

    def get_endpoint_status(self, ep):
        return {"status": "online" if self.online else "offline"}

    def stop_endpoint(self, ep):
        self.stops += 1
        self.online = False
        return {"lock_expiration_timestamp": restart.time.time() + self.lock_in}


def test_restart_anywhere_stops_via_globus_waits_for_lock_then_starts():
    client, slept, sent = StoppableClient(lock_in=60), [], []

    def ssh(alias, cmd):
        sent.append((alias, cmd))
        client.online = True
        return "started-on-derecho2\n"
    out = restart.restart_anywhere("derecho", CFG, client, ssh=ssh, sleep=slept.append)
    assert client.stops == 1 and out == "started-on-derecho2"
    assert any(s > 55 for s in slept)  # waited out the reconnect lock
    (alias, cmd), = sent
    assert alias == "delta" and "start --detach gcx" in cmd and " stop " not in cmd


def test_restart_anywhere_skips_the_stop_when_already_offline():
    client = StoppableClient(online=False)

    def ssh(alias, cmd):
        client.online = True
        return "started-on-n1"
    restart.restart_anywhere("delta", CFG, client, ssh=ssh, sleep=lambda s: None)
    assert client.stops == 0


def test_restart_anywhere_refuses_if_a_copy_still_runs_here():
    client = StoppableClient(online=False)
    with pytest.raises(restart.EndpointOffline, match="still running"):
        restart.restart_anywhere("delta", CFG, client, ssh=lambda a, c: "already-running-here",
                                 sleep=lambda s: None)
