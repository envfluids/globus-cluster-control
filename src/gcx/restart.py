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


STOP_WAIT_S = 120


def _ssh_run(alias, cmd):
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, cmd], capture_output=True,
                       text=True, timeout=180, stdin=subprocess.DEVNULL)
    if p.returncode:
        raise EndpointOffline(f"ssh {alias} failed: {p.stderr.strip()[-300:]}")
    return p.stdout


def restart_anywhere(cluster, cfg, client, ssh=_ssh_run, sleep=time.sleep):
    """Restart an endpoint on a cluster without a cron keepalive.

    `globus-compute-endpoint stop` over SSH is wrong on round-robin logins: SSH
    may land on another node than the one running the endpoint, where `stop`
    misses it (and acts on a pid file from the other node in the shared home),
    and the following `start` would make a second copy. So: stop it through the
    Globus service, which reaches it wherever it runs; wait until it is offline
    and Globus's reconnect lock has passed; then start it on the node SSH lands
    on (start_cmd refuses if a copy is still running there).
    """
    ep = cfg["endpoint"]
    alias = cfg.get("ssh", cluster)
    if status(client, ep) == "online":
        lock = (client.stop_endpoint(ep) or {}).get("lock_expiration_timestamp") or 0
        deadline = time.time() + STOP_WAIT_S
        while status(client, ep) == "online":
            if time.time() > deadline:
                raise EndpointOffline(f"the {cluster} endpoint did not stop within {STOP_WAIT_S} s")
            sleep(5)
        wait = lock - time.time()
        if wait > 0:
            sleep(wait + 2)  # Globus refuses reconnects until the lock expires
    out = ssh(alias, start_cmd(cfg)).strip()
    if "already-running-here" in out:
        raise EndpointOffline(f"an endpoint process is still running on {alias}'s login node "
                              f"after the stop; check `gcx doctor {cluster}`")
    deadline = time.time() + ONLINE_WAIT_S
    while status(client, ep) != "online":
        if time.time() > deadline:
            raise EndpointOffline(f"the {cluster} endpoint did not come online within "
                                  f"{ONLINE_WAIT_S} s of restarting")
        sleep(5)
    return out
