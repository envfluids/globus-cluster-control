"""The laptop watcher: decisions, de-duplicated notifications, scheduler files.
No Globus, SSH or real scheduler: everything is injected or rendered only."""

import plistlib

import pytest

from gcx import config, restart, watch

H = 3600


# ---- decide ------------------------------------------------------------------

def test_online_is_quiet():
    assert watch.decide("delta", "on-use", "online", {}, 0, False) == ("none", "")


def test_on_use_offline_restarts_when_ssh_is_up():
    assert watch.decide("delta", "on-use", "offline", {}, 0, True)[0] == "restart"


def test_on_use_offline_without_ssh_notifies_once_then_daily():
    a, msg = watch.decide("delta", "on-use", "offline", {"status": "online"}, 100, False)
    assert a == "notify" and "gcx login delta" in msg
    prev = {"status": "offline", "since": 100, "notified": 100}
    assert watch.decide("delta", "on-use", "offline", prev, 100 + 2 * H, False)[0] == "none"
    assert watch.decide("delta", "on-use", "offline", prev, 100 + 25 * H, False)[0] == "notify"


def test_cron_cluster_gets_a_grace_period_and_is_never_restarted():
    prev = {"status": "offline", "since": 0}  # in grace: not yet notified
    assert watch.decide("midway3", "failover", "offline", {}, 0, True) == ("none", "")
    assert watch.decide("midway3", "failover", "offline", prev, 300, True) == ("none", "")
    a, msg = watch.decide("midway3", "failover", "offline", prev, 900, True)
    assert a == "notify" and "cron keeps it alive" in msg and "gcx doctor midway3" in msg


def test_unsupervised_cluster_points_to_setup():
    a, msg = watch.decide("x", "none", "offline", {}, 0, False)
    assert a == "notify" and "gcx setup x" in msg


def test_back_online_announced_only_if_the_outage_was_reported():
    reported = {"status": "offline", "since": 10, "notified": 10}
    silent = {"status": "offline", "since": 10, "notified": 0}  # a short cron restart
    assert watch.decide("d", "on-use", "online", reported, 99, False)[0] == "notify"
    assert watch.decide("d", "failover", "online", silent, 99, False) == ("none", "")


# ---- run_once -----------------------------------------------------------------

class Client:
    def __init__(self, statuses):
        self.statuses = statuses

    def get_endpoint_status(self, ep):
        return {"status": self.statuses[ep]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    config.save("delta", {"endpoint": "d", "ssh": "delta", "keepalive": {"mode": "on-use"}})
    config.save("midway3", {"endpoint": "m", "keepalive": {"mode": "failover"}})
    sent = []
    return sent


def run(env, statuses, now, ssh_up=False, monkeypatch=None):
    return watch.run_once(client_factory=lambda: Client(statuses), now=now,
                          ssh_up=lambda alias: ssh_up, notifier=lambda t, m: env.append(m))


def test_pass_records_state_and_notifies_once(env):
    assert run(env, {"d": "offline", "m": "online"}, 1000) == 1
    assert len(env) == 1 and "gcx login delta" in env[0]
    run(env, {"d": "offline", "m": "online"}, 1000 + 1800)
    assert len(env) == 1  # still down, already told
    st = watch.json.loads(watch.state_file().read_text())
    assert st["delta"]["status"] == "offline" and st["midway3"]["status"] == "online"
    assert "delta: offline (on-use)" in watch.log_file().read_text()


def test_pass_restarts_on_use_cluster_when_ssh_is_up(env, monkeypatch):
    started = []
    monkeypatch.setattr(restart, "ensure_online", lambda c, cfg, client: started.append(c))
    assert run(env, {"d": "offline", "m": "online"}, 1000, ssh_up=True) == 0
    assert started == ["delta"] and "restarted it over SSH" in env[0]


def test_failed_restart_is_reported(env, monkeypatch):
    def boom(c, cfg, client):
        raise restart.EndpointOffline("did not come online")
    monkeypatch.setattr(restart, "ensure_online", boom)
    assert run(env, {"d": "offline", "m": "online"}, 1000, ssh_up=True) == 1
    assert "restart failed" in env[0]


def test_unreachable_globus_notifies_once_a_day(env):
    def broken():
        raise ConnectionError("no network")
    for t in (0, 1800, 25 * H):
        watch.run_once(client_factory=broken, now=t, notifier=lambda a, m: env.append(m))
    assert len(env) == 2 and "cannot reach Globus Compute" in env[0]


# ---- scheduler files ----------------------------------------------------------

def test_launchd_plist():
    d = plistlib.loads(watch.launchd_plist(30, exe="/Users/a/.local/bin/gcx").encode())
    assert d["Label"] == watch.LABEL and d["StartInterval"] == 1800 and d["RunAtLoad"]
    assert d["ProgramArguments"] == ["/Users/a/.local/bin/gcx", "watch", "run"]
    assert "/usr/bin" in d["EnvironmentVariables"]["PATH"]


def test_systemd_units():
    service, timer = watch.systemd_units(15, exe="/home/a/.local/bin/gcx")
    assert "ExecStart=/home/a/.local/bin/gcx watch run" in service and "Type=oneshot" in service
    assert "OnUnitActiveSec=15min" in timer and "Persistent=true" in timer


def test_cron_install_replaces_only_its_own_line(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(watch, "scheduler", lambda: "cron")
    monkeypatch.setattr(watch, "gcx_path", lambda: "/usr/local/bin/gcx")
    tab = {"text": "0 3 * * * backup.sh\n"}

    def fake(argv, input=None, **kw):
        if argv == ["crontab", "-l"]:
            return type("P", (), {"stdout": tab["text"], "returncode": 0})()
        tab["text"] = input
        return type("P", (), {"stdout": "", "returncode": 0})()
    monkeypatch.setattr(watch.subprocess, "run", fake)
    watch.install(30)
    watch.install(30)
    assert tab["text"].count(watch.CRON_TAG) == 1 and "backup.sh" in tab["text"]
    assert watch.installed()
    watch.uninstall()
    assert tab["text"] == "0 3 * * * backup.sh\n"


# ---- setup offer ------------------------------------------------------------

def test_setup_offers_watcher_only_when_missing(monkeypatch, capsys):
    from gcx import profiles
    from gcx.setup import menu, steps
    from gcx.setup.remote import Remote
    c = steps.Ctx("delta", profiles.load("delta"), {"keepalive": {"mode": "on-use"}},
                  Remote("delta"), menu.Prompter(True))
    calls = []
    monkeypatch.setattr(watch, "installed", lambda: bool(calls))
    monkeypatch.setattr(watch, "install", lambda *a: calls.append(a) or "launchd")
    steps.offer_watcher(c, assume_yes=True)
    assert calls and "Nothing on this cluster restarts the endpoint" in capsys.readouterr().out
    steps.offer_watcher(c, assume_yes=True)
    assert len(calls) == 1 and "installed; it covers delta" in capsys.readouterr().out


def test_new_outage_after_a_reported_one_is_reported_again():
    prev = {"status": "offline", "since": 5000, "notified": 100}  # notified about an older outage
    assert watch.decide("d", "on-use", "offline", prev, 5100, False)[0] == "notify"
