"""Run a shell command on a cluster login node via Globus Compute.

    gc <cluster> '<shell command>'          # submit, wait, print, exit with rc
    gc <cluster> --submit '<shell command>' # print task id and return
    gc --result <task_id>                   # fetch a previously submitted task
    gc --status <cluster>                   # keepalive state, via Globus Transfer

Every call is a stateless HTTPS request to the Globus Compute service, so a
network drop costs a retry, not a re-login. Clusters are configured in
~/.config/gc-endpoints.json (see endpoints.example.json).

Submission is at-most-once: it is retried only when the request provably never
left this machine. If the connection drops after the request may have been
sent, gc exits with EXIT_AMBIGUOUS rather than risk running the command twice
(a second `sbatch`). Waiting for results is read-only and retried freely.
"""

import argparse
import json
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3.exceptions
from globus_compute_sdk import Client, ShellFunction
from globus_sdk import GlobusAPIError, NetworkError

CONFIG = Path.home() / ".config" / "gc-endpoints.json"
STALE_S = 360  # matches STALE in endpoint/*/keepalive.sh
GLOBUS = str(Path.home() / ".local" / "bin" / "globus")
EXIT_AMBIGUOUS = 75  # EX_TEMPFAIL: submission state unknown, check before resubmitting

# Patch-level Python differences (3.12.x vs 3.12.y) are harmless for ShellFunction.
warnings.filterwarnings("ignore", message=r"\s*Environment differences detected")
# Sandboxing would run each command in a fresh per-task dir, breaking relative
# paths, so it stays off on the endpoint and its per-task notice is dropped.
SANDBOX_NOTICE = "WARNING: Task sandboxing will not work"

# Statuses where the service answered without doing the work.
REJECTED_STATUSES = (429, 503)
# Statuses where it may or may not have done the work.
AMBIGUOUS_STATUSES = (500, 502, 504)


class AmbiguousSubmission(Exception):
    pass


def _requests_error(e):
    """The underlying requests exception of a Globus network error, if any."""
    while e is not None:
        if isinstance(e, requests.RequestException):
            return e
        e = getattr(e, "underlying_exception", None) or e.__cause__
    return None


def never_sent(e):
    """True only if the request certainly did not reach the server.

    Connection refused, DNS failure, connect timeout and an unreachable proxy
    all happen before a request byte is written. A reset or timeout after
    connecting may follow a fully delivered request -- and a proxy dropping its
    tunnel looks exactly like that, so it counts as sent. TLS errors count as
    sent too: urllib3 raises the same SSLError for a failed handshake and for a
    connection lost while reading the response.
    """
    if isinstance(e, GlobusAPIError):
        return e.http_status in REJECTED_STATUSES
    r = _requests_error(e)
    if isinstance(r, requests.ConnectTimeout):
        return True
    if isinstance(r, requests.ConnectionError) and r.args:
        reason = getattr(r.args[0], "reason", None)  # urllib3 MaxRetryError
        return isinstance(reason, (urllib3.exceptions.NewConnectionError,
                                   urllib3.exceptions.ProxyError))
    return False


def transient(e):
    return isinstance(e, NetworkError) or (
        isinstance(e, GlobusAPIError)
        and e.http_status in REJECTED_STATUSES + AMBIGUOUS_STATUSES)


def retry(fn, *args, safe=lambda e: True, attempts=30, **kwargs):
    """Call fn, retrying transient failures that `safe` allows (~10 min total)."""
    delay = 2.0
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not transient(e) or i == attempts - 1:
                raise
            if not safe(e):
                raise AmbiguousSubmission(f"{type(e).__name__}: {e}") from e
            print(f"[gc] network error ({type(e).__name__}), retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


def submit(client, endpoint_id, cmd, walltime):
    fn = ShellFunction("{cmd}", walltime=walltime)
    fid = retry(client.register_function, fn)  # a duplicate registration is harmless
    # The SDK's transport retries any network error itself, POSTs included;
    # turn that off so `never_sent` sees every failure of the submit request.
    web = client._compute_web_client.v3
    with web.retry_config.tune(max_retries=0):
        return retry(_run_on_fresh_connection, client, web,
                     endpoint_id=endpoint_id, function_id=fid, cmd=cmd, safe=never_sent)


def _run_on_fresh_connection(client, web, **kwargs):
    # A pooled keep-alive socket that died during an outage fails on write,
    # which is indistinguishable from a request that was delivered. A fresh
    # connection fails at connect instead, which is provably unsent.
    for adapter in web.transport.session.adapters.values():
        adapter.close()
    return client.run(**kwargs)


def wait(client, task_id, poll=2.0):
    while True:
        task = retry(client.get_task, task_id)
        if not task.get("pending", True):
            return task
        time.sleep(poll)
        poll = min(poll * 1.5, 15.0)


def report(task):
    if "exception" in task:
        print(task["exception"], file=sys.stderr)
        return 1
    res = task["result"]
    sys.stdout.write(res.stdout)
    sys.stderr.writelines(l for l in res.stderr.splitlines(keepends=True)
                          if not l.startswith(SANDBOX_NOTICE))
    return res.returncode


def load_config(cluster):
    entry = json.loads(CONFIG.read_text())[cluster]
    return entry if isinstance(entry, dict) else {"endpoint": entry}


def status(cluster):
    state = load_config(cluster).get("state")
    if not state:
        print(f"no 'state' path configured for {cluster} in {CONFIG}", file=sys.stderr)
        return 1
    out = subprocess.run([GLOBUS, "ls", "-l", "--format", "json", state],
                         capture_output=True, text=True)
    if out.returncode:
        print(out.stderr, file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    files = {e["name"]: (now - datetime.fromisoformat(e["last_modified"])).total_seconds()
             for e in json.loads(out.stdout)["DATA"]}
    holders = [n.removeprefix("owner-is.") for n in files if n.startswith("owner-is.")]
    lease = files.get("owner")
    ok = len(holders) == 1 and lease is not None and lease < STALE_S
    for name, age in sorted(files.items()):
        if name.startswith("heartbeat."):
            flag = "" if age < STALE_S else "  STALE"
            print(f"{name.removeprefix('heartbeat.'):20s} heartbeat {age:5.0f}s ago{flag}")
    print(f"endpoint held by: {', '.join(holders) or 'nobody'}"
          f" (lease {'missing' if lease is None else f'{lease:.0f}s old'})")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(prog="gc", description=__doc__.split("\n\n")[0])
    p.add_argument("cluster", nargs="?")
    p.add_argument("cmd", nargs="?")
    p.add_argument("--submit", action="store_true", help="return task id without waiting")
    p.add_argument("--result", metavar="TASK_ID")
    p.add_argument("--status", metavar="CLUSTER", help="keepalive state via Globus Transfer")
    p.add_argument("--walltime", type=float, default=600, help="seconds before the command is killed")
    a = p.parse_args()

    if a.status:
        sys.exit(status(a.status))
    client = retry(Client)  # the constructor makes a (read-only) version-check request
    if a.result:
        sys.exit(report(wait(client, a.result)))
    if not (a.cluster and a.cmd):
        p.error("need <cluster> and <cmd>, or --result / --status")
    try:
        task_id = submit(client, load_config(a.cluster)["endpoint"], a.cmd, a.walltime)
    except AmbiguousSubmission as e:
        print(f"[gc] connection lost after the request may have been sent ({e}).\n"
              f"[gc] The command may or may not be running on {a.cluster}; check "
              f"(e.g. squeue) before resubmitting.", file=sys.stderr)
        sys.exit(EXIT_AMBIGUOUS)
    if a.submit:
        print(task_id)
        return
    print(f"[gc] task {task_id}", file=sys.stderr)
    sys.exit(report(wait(client, task_id)))


if __name__ == "__main__":
    main()
