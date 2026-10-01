"""`gcx doctor <cluster>`: prove the cluster works and is locked down.

Each check prints PASS/FAIL with a reason; exit 0 only if all pass. Safe to
run at any time: it only reads, plus one harmless raw `echo` that must be
refused once the allowlist is in force.
"""

import hashlib
import json

from globus_compute_sdk import ShellFunction

from gcx import allowlist, config, registry
from gcx.capabilities import build
from gcx.transport import retry, wait


def _run(client, cfg, name, **kwargs):
    fid = cfg["functions"][name]["uuid"]
    task = retry(client.run, endpoint_id=cfg["endpoint"], function_id=fid, **kwargs)
    t = wait(client, task)
    if "exception" in t:
        raise RuntimeError(t["exception"])
    return t["result"]


def checks(client, cluster):
    cfg = config.load(cluster)
    funcs = registry.current(cfg)
    yield "functions registered for current policy", bool(funcs), \
        "ok" if funcs else f"run `gcx register {cluster}`"
    if not funcs:
        return

    ping = _run(client, cfg, "gcx_ping")
    sha = hashlib.sha256(json.dumps(cfg["policy"], sort_keys=True).encode()).hexdigest()
    yield "endpoint answers", True, f"{ping['host']}, Python {ping['python']}"
    yield "endpoint runs this policy", ping["policy_sha256"] == sha, ping["policy_sha256"][:12]

    restricted, live = allowlist.service_state(client, cfg["endpoint"])
    want = {f["uuid"] for f in funcs.values()}
    yield "allowlist in force", restricted and live == want, (
        f"{len(live)} functions" if restricted and live == want else
        "not restricted: run `gcx allowlist {0} --apply`".format(cluster) if not restricted else
        "service allowlist differs from local functions: run `gcx allowlist {0} --apply`".format(cluster))

    try:
        fid = retry(client.register_function, ShellFunction("echo gcx-doctor"))
        task = client.run(endpoint_id=cfg["endpoint"], function_id=fid)
        t = wait(client, task)
        ran = "exception" not in t
        yield "unregistered code refused", not ran, "it RAN" if ran else "refused on the endpoint"
    except Exception as e:  # expected: 403 from the service at submit
        yield "unregistered code refused", True, f"{type(e).__name__}: {str(e)[:80]}"

    if "read" in cfg["policy"]["capabilities"]:
        r = _run(client, cfg, "gcx_read", path="/etc/passwd")
        yield "read outside allowed dirs refused", bool(r.get("refused")), r.get("stderr", "").strip()[:80]

    j = _run(client, cfg, "gcx_jobs")
    yield "scheduler reachable (jobs)", j["rc"] == 0, j.get("stderr", "").strip()[:80] or "ok"


def main(client, cluster):
    ok = True
    for name, passed, detail in checks(client, cluster):
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {name:42s} {detail}")
    return 0 if ok else 1
