"""`gcx watch`: a laptop-side watcher for your endpoints.

Every 30 minutes (launchd on macOS, a systemd user timer -- or crontab -- on
Linux) `gcx watch run` asks the Globus service whether each configured
cluster's endpoint is online. That is one HTTPS status call per cluster: no
tasks, nothing billed, nothing in your job history.

- online                                   -> nothing (or "back online")
- offline, keepalive "on-use", SSH up      -> restart it over SSH, notify
- offline, keepalive "on-use", SSH down    -> notify: run `gcx login <c>`
- offline > 10 min where cron keeps it up  -> notify: the keepalive failed
- offline, no keepalive                    -> notify: run `gcx setup <c>`
- the laptop's Globus login lapsed         -> notify: run any gcx command

It notifies on changes only, plus one reminder a day while something stays
down. It never restarts an endpoint that cron supervises (that would race the
keepalive). State: ~/.config/gcx/watch-state.json; log: watch.log beside it.
"""

import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from gcx import config, restart

LABEL = "org.envfluids.gcx.watch"
INTERVAL_MIN = 30
CRON_GRACE_S = 600          # a cron keepalive restarts within ~6 min; allow 10
REMIND_S = 24 * 3600        # while something stays down, remind once a day
CRON_MODES = ("failover", "single")


def state_file():
    return config.CONFIG_DIR / "watch-state.json"


def log_file():
    return config.CONFIG_DIR / "watch.log"


def log(msg):
    log_file().parent.mkdir(parents=True, exist_ok=True)
    with open(log_file(), "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def notify(title, message):
    """Desktop notification where available; always logged."""
    log(f"NOTIFY {title}: {message}")
    try:
        if platform.system() == "Darwin":
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


# --- decisions (pure, unit-tested) ------------------------------------------

def decide(cluster, mode, status, prev, now, ssh_up):
    """What to do about one cluster this pass.

    Returns (action, message): action is "none", "notify" or "restart"
    ("restart" reports its own outcome). prev is the stored state
    {"status", "since", "notified"}: since = when the current status began,
    notified = when we last notified about this cluster.
    """
    prev = prev or {}
    was, notified = prev.get("status"), prev.get("notified")  # None: never notified
    if status == "online":
        # Only announce recovery from an outage we told the user about.
        reported = was == "offline" and notified is not None and notified >= prev.get("since", now)
        return ("notify", f"{cluster} endpoint is back online") if reported else ("none", "")
    since = prev.get("since", now) if was == "offline" else now
    down_for = now - since
    # New outage not yet reported, or a daily reminder while it lasts.
    due = notified is None or notified < since or now - notified >= REMIND_S
    if mode == "on-use":
        if ssh_up:
            return "restart", ""
        msg = (f"{cluster} endpoint is offline (login node rebooted?). "
               f"Run: gcx login {cluster} -- the next check restarts it")
    elif mode in CRON_MODES:
        if down_for < CRON_GRACE_S:
            return "none", ""  # cron restarts it within minutes; give it time
        msg = (f"{cluster} endpoint has been offline for {int(down_for // 60)} min although cron "
               f"keeps it alive. Check: gcx doctor {cluster}")
    else:
        msg = f"{cluster} endpoint is offline and nothing restarts it. Run: gcx setup {cluster}"
    return ("notify", msg) if due else ("none", "")


# --- one pass ------------------------------------------------------------------

def run_once(client_factory=None, clusters=None, now=None, ssh_up=None, notifier=notify):
    """Check every cluster once; returns the number still offline."""
    now = time.time() if now is None else now
    clusters = config.configured() if clusters is None else clusters
    ssh_up = ssh_up or restart._master_up
    sf = state_file()
    state = json.loads(sf.read_text()) if sf.exists() else {}
    try:
        client = (client_factory or _client)()
    except Exception as e:  # tokens lapsed, network down: say so once a day
        last = state.get("_client", {}).get("notified")
        if last is None or now - last >= REMIND_S:
            notifier("gcx", f"cannot reach Globus Compute ({type(e).__name__}). "
                            f"If it says login, run any gcx command in a terminal")
            state["_client"] = {"notified": now}
            _save(sf, state)
        log(f"client error: {type(e).__name__}: {str(e)[:200]}")
        return len(clusters)
    state.pop("_client", None)
    offline = 0
    for c in clusters:
        try:
            cfg = config.load(c)
            mode = (cfg.get("keepalive") or {}).get("mode", "none")
            st = restart.status(client, cfg["endpoint"])
        except Exception as e:
            log(f"{c}: status check failed: {type(e).__name__}: {str(e)[:200]}")
            continue
        prev = state.get(c, {})
        action, msg = decide(c, mode, st, prev, now, ssh_up(cfg.get("ssh", c))
                             if st != "online" and mode == "on-use" else False)
        if action == "restart":
            try:
                restart.ensure_online(c, cfg, client)
                st, msg = "online", f"{c} endpoint was offline; restarted it over SSH"
            except restart.EndpointOffline as e:
                msg = f"{c} endpoint is offline and the restart failed: {e}"
            notifier("gcx", msg)
            action = "notified"
        elif action == "notify":
            notifier("gcx", msg)
        entry = {"status": st, "since": prev.get("since", now) if prev.get("status") == st else now,
                 "checked": now, "mode": mode}
        entry["notified"] = now if action in ("notify", "notified") else prev.get("notified")
        state[c] = entry
        log(f"{c}: {st} ({mode})" + (f" -> {msg}" if action != "none" else ""))
        offline += st != "online"
    _save(sf, state)
    return offline


def _save(sf, state):
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(sf)


def _client():
    from gcx.cli import _client as make
    return make()


# --- scheduling -----------------------------------------------------------------

def gcx_path():
    # The PATH entry itself (~/.local/bin/gcx), not where its symlink points:
    # that is inside uv's tool directory and may move on a reinstall.
    p = shutil.which("gcx") or sys.argv[0]
    return str(Path(p).absolute())


def launchd_plist(interval_min=INTERVAL_MIN, exe=None):
    home = Path.home()
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [exe or gcx_path(), "watch", "run"],
        # launchd runs a missed interval once on wake, so a sleeping laptop catches up.
        "StartInterval": int(interval_min) * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(log_file()),
        "StandardErrorPath": str(log_file()),
        "EnvironmentVariables": {
            "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "ProcessType": "Background",
    }).decode()


def systemd_units(interval_min=INTERVAL_MIN, exe=None):
    service = (f"[Unit]\nDescription=gcx endpoint watcher\n\n[Service]\nType=oneshot\n"
               f"ExecStart={exe or gcx_path()} watch run\n")
    timer = (f"[Unit]\nDescription=Run the gcx endpoint watcher every {interval_min} min\n\n"
             f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec={interval_min}min\nPersistent=true\n\n"
             f"[Install]\nWantedBy=timers.target\n")
    return service, timer


CRON_TAG = "# gcx-watch"


def _paths():
    home = Path.home()
    return {
        "plist": home / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        "service": home / ".config" / "systemd" / "user" / "gcx-watch.service",
        "timer": home / ".config" / "systemd" / "user" / "gcx-watch.timer",
    }


def scheduler():
    if platform.system() == "Darwin":
        return "launchd"
    if shutil.which("systemctl") and subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0:
        return "systemd"
    return "cron"


def installed():
    kind, p = scheduler(), _paths()
    if kind == "launchd":
        return p["plist"].exists()
    if kind == "systemd":
        return p["timer"].exists()
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    return CRON_TAG in out


def install(interval_min=INTERVAL_MIN):
    kind, p = scheduler(), _paths()
    if kind == "launchd":
        p["plist"].parent.mkdir(parents=True, exist_ok=True)
        p["plist"].write_text(launchd_plist(interval_min))
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", domain, str(p["plist"])],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    elif kind == "systemd":
        service, timer = systemd_units(interval_min)
        p["service"].parent.mkdir(parents=True, exist_ok=True)
        p["service"].write_text(service)
        p["timer"].write_text(timer)
        for cmd in (["daemon-reload"], ["enable", "--now", "gcx-watch.timer"]):
            subprocess.run(["systemctl", "--user", *cmd], check=True, capture_output=True)
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        keep = "".join(l for l in cur.splitlines(True) if CRON_TAG not in l)
        line = f"*/{interval_min} * * * * {gcx_path()} watch run {CRON_TAG}\n"
        subprocess.run(["crontab", "-"], input=keep + line, text=True, check=True)
    log(f"installed ({kind}, every {interval_min} min)")
    return kind


def uninstall():
    kind, p = scheduler(), _paths()
    if kind == "launchd":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        p["plist"].unlink(missing_ok=True)
    elif kind == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", "gcx-watch.timer"],
                       capture_output=True)
        p["service"].unlink(missing_ok=True)
        p["timer"].unlink(missing_ok=True)
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        keep = "".join(l for l in cur.splitlines(True) if CRON_TAG not in l)
        subprocess.run(["crontab", "-"], input=keep, text=True, check=True)
    log(f"uninstalled ({kind})")
    return kind


def show_status(lines=8):
    kind = scheduler()
    print(f"watcher: {'installed' if installed() else 'not installed'} ({kind})")
    sf = state_file()
    state = json.loads(sf.read_text()) if sf.exists() else {}
    for c, e in sorted(state.items()):
        if c.startswith("_"):
            continue
        when = datetime.fromtimestamp(e.get("checked", 0)).strftime("%m-%d %H:%M")
        print(f"  {c:10s} {e.get('status', '?'):8s} ({e.get('mode', '?')}), checked {when}")
    if log_file().exists():
        print("log:")
        for l in log_file().read_text().splitlines()[-lines:]:
            print("  " + l)
