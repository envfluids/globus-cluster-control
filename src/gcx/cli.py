"""gcx: run job-control commands on cluster login nodes via Globus Compute.

    gcx <cluster> jobs [ID ...]                    your queued/running jobs
    gcx <cluster> history [ID ...] [--since DATE]  finished jobs (sacct / qstat -x)
    gcx <cluster> queues                           partitions / queues
    gcx <cluster> submit SCRIPT [-A ACCT] [-p QUEUE] [-q QOS] [-t TIME] [--after ID ...] [-J NAME]
    gcx <cluster> cancel ID [ID ...]
    gcx <cluster> ls [PATH]          gcx <cluster> du [PATH] [-d N]
    gcx <cluster> tail PATH [-n N]   gcx <cluster> head PATH [-n N]
    gcx <cluster> sh 'CMD'           arbitrary shell, only if enabled for the cluster
    gcx <cluster> ping               endpoint host, Python and policy hash

    gcx status <cluster>             which login node holds the endpoint (via Globus Transfer)
    gcx result <task-id>             collect a task started with --no-wait
    gcx register <cluster>           register the cluster's functions from its policy
    gcx allowlist <cluster> [--apply | --off]   restrict the endpoint to those functions
    gcx doctor <cluster>             check that the cluster works and is locked down
    gcx setup <cluster> [--dry-run] [--yes]   install / update everything, then test it
    gcx ssh-config [cluster ...] [--user U] [--apply]   SSH aliases with shared connections
    gcx login [cluster ...] [--refresh]   open those connections (you answer MFA), once a day

Paths are relative to your cluster home unless absolute; ~ and $VARS expand on
the cluster (an unquoted ~ that your laptop shell expanded is mapped back). Add --json for machine-readable output, --no-wait to print the
task id and return.

A network drop costs a retry, not a re-login. submit, cancel and sh are sent
at-most-once: if the connection drops after the request may have been sent,
gcx exits 75 rather than risk running it twice; check `jobs` before retrying.
"""

import argparse
import json
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from globus_compute_sdk import Client, ShellFunction
from globus_compute_sdk.serialize import JSONData

from gcx import allowlist, config, doctor, profiles, registry
from gcx.capabilities.build import MUTATING
from gcx.transport import EXIT_AMBIGUOUS, AmbiguousSubmission, retry, run_at_most_once, wait

STALE_S = 360  # matches STALE in keepalive.sh

# Patch-level Python differences (3.12.x vs 3.12.y) are harmless for ShellFunction.
warnings.filterwarnings("ignore", message=r"\s*Environment differences detected")
# Sandboxing would run each command in a fresh per-task dir, breaking relative
# paths, so it stays off on the endpoint and its per-task notice is dropped.
SANDBOX_NOTICE = "WARNING: Task sandboxing will not work"


def _verbs():
    """verb -> (function name, argparse builder, kwargs from parsed args)."""
    def ids(p):
        p.add_argument("ids", nargs="*")

    def submit(p):
        p.add_argument("script")
        p.add_argument("-A", "--account")
        p.add_argument("-p", "--queue", "--partition", dest="queue")
        p.add_argument("-q", "--qos")
        p.add_argument("-t", "--walltime")
        p.add_argument("--after", nargs="+", metavar="ID", help="start after these jobs succeed")
        p.add_argument("-J", "--job-name")

    def lines(p):
        p.add_argument("path")
        p.add_argument("-n", "--lines", type=int, default=200)

    def path(p):
        p.add_argument("path", nargs="?", default="~")

    def du(p):
        path(p)
        p.add_argument("-d", "--depth", type=int, default=1)

    def history(p):
        ids(p)
        p.add_argument("--since", help="YYYY-MM-DD[THH:MM[:SS]]")

    def sh(p):
        p.add_argument("cmd")
        p.add_argument("--walltime", type=float, default=600)

    return {
        "ping": ("gcx_ping", None, lambda a: {}),
        "jobs": ("gcx_jobs", ids, lambda a: {"job_ids": a.ids or None}),
        "history": ("gcx_history", history, lambda a: {"job_ids": a.ids or None, "since": a.since}),
        "queues": ("gcx_queues", None, lambda a: {}),
        "submit": ("gcx_submit", submit, lambda a: {
            "script": a.script, "account": a.account, "queue": a.queue, "qos": a.qos,
            "walltime": a.walltime, "depends_on": a.after, "job_name": a.job_name}),
        "cancel": ("gcx_cancel", lambda p: p.add_argument("ids", nargs="+"),
                   lambda a: {"job_ids": a.ids}),
        "ls": ("gcx_ls", path, lambda a: {"path": a.path}),
        "du": ("gcx_du", du, lambda a: {"path": a.path, "depth": a.depth}),
        "tail": ("gcx_read", lines, lambda a: {"path": a.path, "mode": "tail", "lines": a.lines}),
        "head": ("gcx_read", lines, lambda a: {"path": a.path, "mode": "head", "lines": a.lines}),
        "sh": ("gcx_shell", sh, lambda a: {"cmd": a.cmd, "walltime": a.walltime}),
    }


VERBS = _verbs()


PATH_ARGS = ("path", "script")


def _cluster_path(path):
    """Undo the laptop shell's expansion of an unquoted ~ (`gcx c ls ~/x`)."""
    home = str(Path.home())
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _client():
    # JSON for arguments, so the endpoint never unpickles anything we send.
    return retry(Client, data_serialization_strategy=JSONData())


def _human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def render(verb, res, as_json):
    """Print a task result; return the exit code."""
    if not isinstance(res, dict):  # pilot ShellFunction result
        sys.stdout.write(res.stdout)
        sys.stderr.writelines(l for l in res.stderr.splitlines(keepends=True)
                              if not l.startswith(SANDBOX_NOTICE))
        return res.returncode
    if as_json:
        print(json.dumps(res, indent=1))
        return res.get("rc", 0)
    if verb == "ping":
        print(json.dumps(res, indent=1))
        return 0
    if "entries" in res:
        for e in res["entries"]:
            mark = {"dir": "/", "link": "@"}.get(e["type"], "")
            when = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
            print(f"{_human(e['size']):>7}  {when}  {e['name']}{mark}")
        if res.get("truncated"):
            print("[gcx] listing truncated", file=sys.stderr)
        return 0
    if verb == "submit" and res.get("job_id"):
        print(res["job_id"])
        sys.stderr.write(res.get("stderr", ""))
        return 0
    sys.stdout.write(res.get("stdout", ""))
    sys.stderr.write(res.get("stderr", ""))
    if res.get("truncated"):
        print("[gcx] output truncated", file=sys.stderr)
    return res.get("rc", 0)


def call_capability(cluster, fname, no_wait=False, client=None, **kwargs):
    """Run one registered function; returns its result dict (or the task id).

    Raises AmbiguousSubmission for state-changing calls whose delivery is unknown.
    """
    cfg = config.load(cluster)
    funcs = cfg.get("functions") or {}
    if fname not in funcs:
        raise NotEnabled(cluster, fname, funcs)
    client = client or _client()
    fid = funcs[fname]["uuid"]
    if fname in MUTATING:
        task_id = run_at_most_once(client, cfg["endpoint"], fid, **kwargs)
    else:  # read-only: a duplicate run is harmless
        task_id = retry(client.run, endpoint_id=cfg["endpoint"], function_id=fid, **kwargs)
    if no_wait:
        return task_id
    task = wait(client, task_id)
    if "exception" in task:
        return {"rc": 1, "stdout": "", "stderr": task["exception"] + "\n"}
    return task["result"]


class NotEnabled(Exception):
    def __init__(self, cluster, fname, funcs):
        verb = next((v for v, (f, *_) in VERBS.items() if f == fname), fname)
        enabled = sorted(v for v, (f, *_) in VERBS.items() if f in funcs)
        super().__init__(f"`{verb}` is not enabled on {cluster}"
                         + (f" (enabled: {', '.join(enabled)})" if enabled else
                            f"; run `gcx register {cluster}`"))


def call(cluster, verb, kwargs, as_json=False, no_wait=False):
    fname = VERBS[verb][0]
    funcs = config.load(cluster).get("functions") or {}
    try:
        if verb == "sh" and not funcs:
            # Pilot path: no functions registered yet, so use a plain ShellFunction.
            client = _client()
            fid = retry(client.register_function, ShellFunction("{cmd}", walltime=kwargs["walltime"]))
            task_id = run_at_most_once(client, config.load(cluster)["endpoint"], fid, cmd=kwargs["cmd"])
            if no_wait:
                print(task_id)
                return 0
            return collect(client, task_id, verb, as_json)
        res = call_capability(cluster, fname, no_wait=no_wait, **kwargs)
    except NotEnabled as e:
        sys.exit(f"[gcx] {e}")
    except AmbiguousSubmission as e:
        print(f"[gcx] connection lost after the request may have been sent ({e}).\n"
              f"[gcx] `{verb}` may or may not have run on {cluster}; check "
              f"(`gcx {cluster} jobs`) before retrying.", file=sys.stderr)
        return EXIT_AMBIGUOUS
    if no_wait:
        print(res)
        return 0
    return render(verb, res, as_json)


def collect(client, task_id, verb=None, as_json=False):
    task = wait(client, task_id)
    if "exception" in task:
        print(task["exception"], file=sys.stderr)
        return 1
    return render(verb, task["result"], as_json)


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


GLOBAL = ("status", "result", "register", "allowlist", "doctor", "setup")


def _global(argv, prog):
    p = argparse.ArgumentParser(prog=f"{prog} {argv[0]}")
    p.add_argument("target")
    p.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    if argv[0] == "setup":
        p.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
        p.add_argument("--yes", action="store_true", help="accept every default (no questions)")
        p.add_argument("--no-test-job", action="store_true", help="skip the end-to-end test job")
    if argv[0] == "allowlist":
        g = p.add_mutually_exclusive_group()
        g.add_argument("--apply", action="store_true", help="push the config and restart the endpoint")
        g.add_argument("--off", action="store_true", help="remove the allowlist (rollback); implies --apply")
    a = p.parse_args(argv[1:])
    if argv[0] == "status":
        return status(a.target)
    if argv[0] == "result":
        return collect(_client(), a.target, as_json=a.json)
    if argv[0] == "doctor":
        return doctor.main(_client(), a.target)
    if argv[0] == "setup":
        from gcx.setup import steps
        try:
            return steps.run(a.target, dry_run=a.dry_run, assume_yes=a.yes,
                             test_job=False if a.no_test_job or a.dry_run else None)
        except (profiles.UnknownCluster, steps.RemoteError) as e:
            sys.exit(f"[gcx] {e}")
    if argv[0] == "allowlist":
        try:
            return allowlist.run(_client(), a.target, apply=a.apply or a.off, off=a.off)
        except allowlist.AllowlistError as e:
            sys.exit(f"[gcx] {e}")
    funcs, changed = registry.register(_client(), a.target, force=a.force)
    print(("registered" if changed else "unchanged") + f": {', '.join(sorted(funcs))}")
    if changed:
        print(f"[gcx] new function ids: run `gcx allowlist {a.target} --apply` if the "
              f"allowlist is in force, or calls will be refused", file=sys.stderr)
    return 0


def login(args):
    """Run the bundled morning-login over your configured clusters (MFA prompts)."""
    import os
    from importlib import resources
    script = str(resources.files("gcx.data").joinpath("morning-login"))
    env = dict(os.environ,
               GCX_CLUSTERS=" ".join(config.configured()),
               GCX_SINGLE_USE_MFA=" ".join(n for n in profiles.available()
                                           if profiles.load(n)["mfa_single_use"]))
    os.execvpe("bash", ["bash", script, *args], env)


def ssh_config(args):
    from gcx import sshconfig
    p = argparse.ArgumentParser(prog="gcx ssh-config",
                                description="Write SSH aliases for clusters into your SSH config.")
    p.add_argument("clusters", nargs="*", help="default: your configured clusters")
    p.add_argument("--user", help="cluster username (default: saved per cluster, else local user)")
    p.add_argument("--apply", action="store_true", help="write the file (default: show the diff)")
    p.add_argument("--file", help=argparse.SUPPRESS)  # tests / previews
    a = p.parse_args(args)
    clusters = a.clusters or config.configured()
    if not clusters:
        sys.exit(f"[gcx] name the clusters to add; known: {', '.join(profiles.available())}")
    pairs = []
    for c in clusters:
        profiles.load(c)  # unknown cluster -> clear error
        try:
            cfg = config.load(c)
        except config.NotConfigured:
            cfg = {}
        user = a.user or cfg.get("user") or sshconfig.default_user()
        pairs.append((c, user))
        if a.apply and not a.file and (cfg.get("user") != user or "ssh" not in cfg):
            cfg.update(user=user, ssh=cfg.get("ssh", c))
            config.save(c, cfg)
    return sshconfig.run(pairs, path=a.file, apply=a.apply)


def main(argv=None, prog="gcx"):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Pilot spellings: --status C, --result T.
    if argv[:1] in (["--status"], ["--result"]):
        argv[0] = argv[0][2:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if argv[0] == "login":
        login(argv[1:])
    if argv[0] == "ssh-config":
        try:
            sys.exit(ssh_config(argv[1:]))
        except profiles.UnknownCluster as e:
            sys.exit(f"[gcx] {e}")
    try:
        if argv[0] in GLOBAL:
            sys.exit(_global(argv, prog))
        cluster, rest = argv[0], argv[1:]
        if not rest:
            sys.exit(f"[gcx] what should run on {cluster}? See `{prog} --help`.")
        if rest[0] not in VERBS:
            # Pilot form: gcx <cluster> '<shell command>'.
            print(f"[gcx] treating this as `{prog} {cluster} sh '...'`; say `sh` explicitly",
                  file=sys.stderr)
            rest = ["sh"] + rest
        verb = rest[0]
        p = argparse.ArgumentParser(prog=f"{prog} {cluster} {verb}")
        if VERBS[verb][1]:
            VERBS[verb][1](p)
        p.add_argument("--json", action="store_true", help="print the raw result")
        p.add_argument("--no-wait", action="store_true", help="print the task id and return")
        a = p.parse_args(rest[1:])
        kwargs = VERBS[verb][2](a)
        for k in PATH_ARGS:
            if isinstance(kwargs.get(k), str):
                kwargs[k] = _cluster_path(kwargs[k])
        sys.exit(call(cluster, verb, kwargs, as_json=a.json, no_wait=a.no_wait))
    except config.NotConfigured as e:
        sys.exit(f"[gcx] {e}. Configured: {', '.join(config.configured()) or 'none'}")


def legacy_main():
    """`gc`, the pilot's name. Kept until colleagues' scripts move to gcx."""
    print("[gcx] `gc` is deprecated; use `gcx`", file=sys.stderr)
    main(prog="gc")


if __name__ == "__main__":
    main()
