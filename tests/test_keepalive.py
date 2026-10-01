"""keepalive.sh's failover and single-node logic, with two simulated login nodes.

Shims on PATH replace `hostname` (returns $FAKE_HOST), `pgrep`/`pkill` and the
endpoint binary: "running on node X" is the file run/X, so the nodes share one
tmp "home" (as the real nodes share /home) but have separate process tables.
"""

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "src" / "gcx" / "data" / "keepalive.sh"

SHIMS = {
    "hostname": 'echo "$FAKE_HOST"',
    "pgrep": '[ -e "$FAKE_ROOT/run/$FAKE_HOST" ]',
    "pkill": 'rm -f "$FAKE_ROOT/run/$FAKE_HOST"',
    "fake-gce": '''
cmd=$1; shift; [ "$1" = --detach ] && shift
echo "$cmd $1 on $FAKE_HOST" >> "$FAKE_ROOT/gce.log"
case $cmd in
  start) [ -e "$FAKE_ROOT/broken" ] && exit 1; mkdir -p "$FAKE_ROOT/run"; touch "$FAKE_ROOT/run/$FAKE_HOST" ;;
  stop)  rm -f "$FAKE_ROOT/run/$FAKE_HOST" ;;
esac''',
}


class Cluster:
    def __init__(self, root, mode, primary, backup=""):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in SHIMS.items():
            f = self.bin / name
            f.write_text("#!/bin/bash\n" + body + "\n")
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.home = root / "home"
        (self.home / "gcx").mkdir(parents=True)
        self.state = self.home / "gcx" / "state"
        script = self.home / "gcx" / "keepalive.sh"
        script.write_text(SCRIPT.read_text())
        script.chmod(0o755)
        self.script = script
        (self.home / "gcx" / "keepalive.env").write_text(
            f"MODE={mode}\nPRIMARY={primary}\nBACKUP={backup}\nEP=ep\n"
            f"GCE={self.bin / 'fake-gce'}\nSTATE={self.state}\nSTALE=360\nSETTLE=0\n")

    def tick(self, node, role=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "USER": "alice",
               "FAKE_HOST": node, "FAKE_ROOT": str(self.root)}
        p = subprocess.run([str(self.script)] + ([role] if role else []), env=env,
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout

    def running(self):
        run = self.root / "run"
        return sorted(p.name for p in run.iterdir()) if run.exists() else []

    def owner(self):
        f = self.state / "owner"
        return f.read_text().strip() if f.exists() else None

    def age(self, name, seconds):
        """Make state/<name> look `seconds` old (a node that stopped ticking)."""
        t = time.time() - seconds
        os.utime(self.state / name, (t, t))


@pytest.fixture
def failover(tmp_path):
    return Cluster(tmp_path, "failover", "login4", "login3")


def test_primary_starts_and_backup_stays_idle(failover):
    c = failover
    rc, out = c.tick("login4", "primary")
    assert rc == 0 and "started endpoint (primary)" in out
    rc, out = c.tick("login3", "backup")
    assert rc == 0 and out == ""  # silent: nothing changed
    assert c.running() == ["login4"] and c.owner() == "login4"
    assert (c.state / "owner-is.login4").exists()


def test_quiet_when_nothing_changes(failover):
    c = failover
    c.tick("login4")
    assert c.tick("login4") == (0, "")


def test_backup_takes_over_only_when_primary_is_stale(failover):
    c = failover
    c.tick("login4")
    c.tick("login3")
    (c.root / "run" / "login4").unlink()   # login4 dies: process gone, cron stops
    c.age("heartbeat.login4", 300)
    assert c.tick("login3") == (0, "")     # 300 s < 360 s: still waits
    c.age("heartbeat.login4", 400)
    c.age("owner", 400)
    rc, out = c.tick("login3")
    assert "started endpoint (primary heartbeat 4" in out
    assert c.running() == ["login3"] and c.owner() == "login3"
    assert not (c.state / "owner-is.login4").exists()


def test_failback_never_runs_two(failover):
    c = failover
    c.tick("login4")
    (c.root / "run" / "login4").unlink()
    c.age("heartbeat.login4", 400)
    c.age("owner", 400)
    c.tick("login3")
    assert c.running() == ["login3"]
    # login4 returns: sees login3's fresh lease and waits
    assert c.tick("login4") == (0, "") and c.running() == ["login3"]
    # login3 sees login4's fresh heartbeat and hands back
    rc, out = c.tick("login3")
    assert "stopped endpoint (primary is back)" in out and c.running() == []
    rc, out = c.tick("login4")
    assert "started endpoint (primary)" in out and c.running() == ["login4"]


def test_restart_request_keeps_lease(failover):
    c = failover
    c.tick("login4")
    (c.state / "restart-request").touch()
    rc, out = c.tick("login4")
    assert "restarted endpoint (requested)" in out
    assert not (c.state / "restart-request").exists()
    assert c.running() == ["login4"] and c.owner() == "login4"
    log = (c.root / "gce.log").read_text().splitlines()
    assert log[-2:] == ["stop ep on login4", "start ep on login4"]


def test_restart_request_ignored_by_node_not_running_it(failover):
    c = failover
    c.tick("login4")
    (c.state / "restart-request").touch()
    assert c.tick("login3") == (0, "")
    assert (c.state / "restart-request").exists()


def test_wrong_node_or_role_refused(failover):
    c = failover
    rc, out = c.tick("login1")
    assert rc == 1 and "has no role" in out
    rc, out = c.tick("login3", "primary")
    assert rc == 1 and "is 'backup'" in out


def test_failed_start_is_reported(failover):
    c = failover
    (c.root / "broken").touch()
    rc, out = c.tick("login4")
    assert "FAILED to start endpoint" in out and c.running() == []


def test_single_mode_restarts_dead_endpoint(tmp_path):
    c = Cluster(tmp_path, "single", "dsi-fe01")
    assert "started endpoint (not running)" in c.tick("dsi-fe01", "single")[1]
    assert c.tick("dsi-fe01") == (0, "")
    (c.root / "run" / "dsi-fe01").unlink()
    assert "started endpoint (not running)" in c.tick("dsi-fe01")[1]
    rc, out = c.tick("dsi-fe02")
    assert rc == 1 and "has no role" in out
