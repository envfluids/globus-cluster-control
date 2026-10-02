"""Restart on use: for clusters where cron is unavailable (keepalive mode "on-use").

Nothing on such a cluster restarts the endpoint after a login-node reboot. So
before a call, gcx asks the Globus service whether the endpoint is online. If
it is offline, and only if your SSH connection to the cluster is already up
(no prompt can appear), gcx starts it over SSH and waits until it is online.
Otherwise it says to run `gcx login <cluster>` and retry.

Guards against starting a second copy: the endpoint must read offline twice,
5 s apart, and no endpoint process may be running on the node SSH lands on.
"""

import subprocess
import sys
import time

from gcx.transport import retry

OFFLINE_CONFIRM_S = 5
ONLINE_WAIT_S = 120


class EndpointOffline(Exception):
    pass


def status(client, endpoint_id):
    return retry(client.get_endpoint_status, endpoint_id).get("status")


def _master_up(alias):
    return subprocess.run(["ssh", "-O", "check", alias], capture_output=True,
                          stdin=subprocess.DEVNULL).returncode == 0


def start_cmd(cfg):
    root, ep = cfg.get("remote_root", "~/.gcx"), cfg.get("endpoint_name", "gcx")
    gce = f"{root}/venv/bin/globus-compute-endpoint"
    return (f'pgrep -u "$USER" -f "^Globus Compute Endpoint .*, {ep}\\)" >/dev/null '
            f"&& {{ echo already-running-here; exit 0; }}; "
            f"cd ~ && rm -f ~/.globus_compute/{ep}/daemon.pid && {gce} start --detach {ep} "
            f"&& echo started-on-$(hostname -s)")


def ensure_online(cluster, cfg, client):
    """Return once the endpoint is online, restarting it if this cluster allows."""
    if cfg.get("keepalive", {}).get("mode") != "on-use":
        return
    ep = cfg["endpoint"]
    if status(client, ep) == "online":
        return
    time.sleep(OFFLINE_CONFIRM_S)
    if status(client, ep) == "online":
        return
    alias = cfg.get("ssh", cluster)
    if not _master_up(alias):
        raise EndpointOffline(
            f"the {cluster} endpoint is offline (e.g. its login node rebooted), and there is "
            f"no SSH connection to restart it with. Run `gcx login {cluster}`, then retry.")
    print(f"[gcx] {cluster} endpoint is offline; restarting it over SSH", file=sys.stderr)
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, start_cmd(cfg)],
                       capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    if p.returncode:
        raise EndpointOffline(f"restarting the {cluster} endpoint failed: {p.stderr.strip()[-300:]}")
    print(f"[gcx] {p.stdout.strip()}; waiting for it to come online", file=sys.stderr)
    deadline = time.time() + ONLINE_WAIT_S
    while time.time() < deadline:
        time.sleep(5)
        if status(client, ep) == "online":
            return
    raise EndpointOffline(f"the {cluster} endpoint did not come online within "
                          f"{ONLINE_WAIT_S} s of restarting; check `gcx doctor {cluster}`")
