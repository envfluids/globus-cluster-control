"""Restrict an endpoint to the cluster's registered gcx functions.

The allowlist lives in the endpoint manager's config.yaml (`allowed_functions`).
It is sent to the Globus Compute service when the endpoint starts, and the
service then refuses any other function with HTTP 403; the endpoint re-checks
locally. Changing it needs a restart, requested through the keepalive
(`restart-request` in the state dir), so whichever login node runs the endpoint
picks it up within one cron tick.

Pushing the file is setup, not day-to-day use, so it goes over SSH.
"""

import difflib
import shlex
import subprocess
import sys
import time

from gcx import config, registry

COMMENT = "# gcx: only these registered functions may run (gcx allowlist)."
POLL_S = 15
TIMEOUT_S = 480  # two cron ticks plus endpoint start-up, with margin


class AllowlistError(Exception):
    pass


def _remote(cfg):
    for key in ("ssh", "endpoint_name", "remote_state"):
        if not cfg.get(key):
            raise AllowlistError(f"cluster config lacks {key!r}")
    return cfg["ssh"], f".globus_compute/{cfg['endpoint_name']}/config.yaml", cfg["remote_state"]


def _ssh(alias, cmd, stdin=None):
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, cmd], input=stdin,
                       capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise AllowlistError(f"ssh {alias} failed: {p.stderr.strip()}")
    return p.stdout


def strip_allowlist(text):
    """config.yaml without any allowed_functions block."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith(COMMENT):
            continue
        if line.startswith("allowed_functions:"):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "-", "\t")) or not line.strip()):
            continue
        skipping = False
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def render(current_text, uuids):
    text = strip_allowlist(current_text)
    if uuids is None:
        return text
    lines = [COMMENT, "allowed_functions:"] + [f"  - {u}" for u in sorted(uuids)]
    return text + "\n".join(lines) + "\n"


def wanted_uuids(cluster, cfg):
    funcs = registry.current(cfg)
    if not funcs:
        raise AllowlistError(f"registered functions are missing or stale; run `gcx register {cluster}`")
    return {f["uuid"] for f in funcs.values()}


def service_state(client, endpoint_id):
    """(restricted, set of allowed function ids) as the Globus service sees it."""
    data = client.get_allowed_functions(endpoint_id)
    return bool(data.get("restricted")), set(map(str, data.get("functions") or []))


def run(client, cluster, apply=False, off=False):
    cfg = config.load(cluster)
    alias, path, state = _remote(cfg)
    want = None if off else wanted_uuids(cluster, cfg)
    old = _ssh(alias, f"cat {shlex.quote(path)}")
    new = render(old, want)
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"{alias}:{path}", "rendered"))
    restricted, live = service_state(client, cfg["endpoint"])
    in_force = (live == want) if want is not None else not restricted
    if not diff and in_force:
        print(f"{cluster}: allowlist already {'off' if off else 'in force'} ({len(live)} functions)")
        return 0
    sys.stdout.write(diff or "(config.yaml already up to date; endpoint not yet restarted)\n")
    if not apply:
        print(f"[gcx] dry run; rerun with --apply to push and restart the {cluster} endpoint")
        return 0
    if diff:
        tmp = path + ".gcx-new"
        _ssh(alias, f"cat > {shlex.quote(tmp)} && mv {shlex.quote(tmp)} {shlex.quote(path)}", stdin=new)
    if cfg.get("keepalive", {}).get("mode") in ("failover", "single"):
        # Whichever login node holds the endpoint restarts it on its next cron tick.
        _ssh(alias, f"touch {state}/restart-request")
        print(f"[gcx] pushed; waiting for the keepalive to restart the endpoint "
              f"(up to {TIMEOUT_S // 60} min)")
    else:
        # No cron keepalive here (on-use / none): restart directly. Clear any stale
        # request so setup does not see a restart as pending forever.
        ep, root = cfg["endpoint_name"], cfg.get("remote_root", "~/.gcx")
        gce = f"{root}/venv/bin/globus-compute-endpoint"
        _ssh(alias, f"rm -f {state}/restart-request; cd ~ && {gce} stop {ep} >/dev/null 2>&1; "
                    f"sleep 5; rm -f ~/.globus_compute/{ep}/daemon.pid; "
                    f"{gce} start --detach {ep}")
        print("[gcx] pushed and restarted the endpoint over SSH; waiting for Globus")
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        time.sleep(POLL_S)
        try:
            restricted, live = service_state(client, cfg["endpoint"])
        except Exception as e:  # transient; keep polling
            print(f"[gcx] poll failed ({type(e).__name__}); retrying", file=sys.stderr)
            continue
        if (want is None and not restricted) or (want is not None and restricted and live == want):
            print(f"[gcx] {cluster}: allowlist {'off' if off else f'in force, {len(live)} functions'}")
            return 0
    raise AllowlistError("timed out waiting for the service to report the new allowlist; "
                         f"check `gcx status {cluster}` and the endpoint log")
