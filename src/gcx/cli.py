"""Run a shell command on a cluster login node via Globus Compute.

    gcx <cluster> '<shell command>'          # submit, wait, print, exit with rc
    gcx <cluster> --submit '<shell command>' # print task id and return
    gcx --result <task_id>                   # fetch a previously submitted task
    gcx --status <cluster>                   # keepalive state, via Globus Transfer

A network drop costs a retry, not a re-login. Submission is at-most-once: if
the connection drops after the request may have been sent, gcx exits 75 rather
than risk running the command twice; check (e.g. squeue) before resubmitting.
"""

import argparse
import json
import subprocess
import sys
import warnings
from datetime import datetime, timezone

from globus_compute_sdk import Client, ShellFunction

from gcx import config
from gcx.transport import EXIT_AMBIGUOUS, AmbiguousSubmission, retry, run_at_most_once, wait

STALE_S = 360  # matches STALE in keepalive.sh

# Patch-level Python differences (3.12.x vs 3.12.y) are harmless for ShellFunction.
warnings.filterwarnings("ignore", message=r"\s*Environment differences detected")
# Sandboxing would run each command in a fresh per-task dir, breaking relative
# paths, so it stays off on the endpoint and its per-task notice is dropped.
SANDBOX_NOTICE = "WARNING: Task sandboxing will not work"


def submit(client, endpoint_id, cmd, walltime):
    fn = ShellFunction("{cmd}", walltime=walltime)
    fid = retry(client.register_function, fn)  # a duplicate registration is harmless
    return run_at_most_once(client, endpoint_id, fid, cmd=cmd)


def report(task):
    if "exception" in task:
        print(task["exception"], file=sys.stderr)
        return 1
    res = task["result"]
    sys.stdout.write(res.stdout)
    sys.stderr.writelines(l for l in res.stderr.splitlines(keepends=True)
                          if not l.startswith(SANDBOX_NOTICE))
    return res.returncode


def status(cluster):
    state = config.load(cluster).get("state")
    if not state:
        print(f"no 'state' path configured for {cluster} in {config.cluster_file(cluster)}",
              file=sys.stderr)
        return 1
    out = subprocess.run([config.globus_cli(), "ls", "-l", "--format", "json", state],
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


def main(prog="gcx"):
    p = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    p.add_argument("cluster", nargs="?")
    p.add_argument("cmd", nargs="?")
    p.add_argument("--submit", action="store_true", help="return task id without waiting")
    p.add_argument("--result", metavar="TASK_ID")
    p.add_argument("--status", metavar="CLUSTER", help="keepalive state via Globus Transfer")
    p.add_argument("--walltime", type=float, default=600, help="seconds before the command is killed")
    a = p.parse_args()

    try:
        if a.status:
            sys.exit(status(a.status))
        if not a.result and not (a.cluster and a.cmd):
            p.error("need <cluster> and <cmd>, or --result / --status")
        endpoint_id = None if a.result else config.load(a.cluster)["endpoint"]
    except config.NotConfigured as e:
        sys.exit(f"[gcx] {e}. Configured: {', '.join(config.configured()) or 'none'}")

    client = retry(Client)  # the constructor makes a (read-only) version-check request
    if a.result:
        sys.exit(report(wait(client, a.result)))
    try:
        task_id = submit(client, endpoint_id, a.cmd, a.walltime)
    except AmbiguousSubmission as e:
        print(f"[gcx] connection lost after the request may have been sent ({e}).\n"
              f"[gcx] The command may or may not be running on {a.cluster}; check "
              f"(e.g. squeue) before resubmitting.", file=sys.stderr)
        sys.exit(EXIT_AMBIGUOUS)
    if a.submit:
        print(task_id)
        return
    print(f"[gcx] task {task_id}", file=sys.stderr)
    sys.exit(report(wait(client, task_id)))


def legacy_main():
    """`gc`, the pilot's name. Kept until colleagues' scripts move to gcx."""
    print("[gcx] `gc` is deprecated; use `gcx`", file=sys.stderr)
    main(prog="gc")


if __name__ == "__main__":
    main()
