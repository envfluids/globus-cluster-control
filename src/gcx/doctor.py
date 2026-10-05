"""`gcx doctor [cluster]`: prove clusters work and are locked down.

With a cluster, every check prints PASS/FAIL with a reason. Without one, all
configured clusters are checked in parallel and each gets one summary line,
with the failing checks spelled out (all checks with -v). Exit 0 only if
everything passes. Safe to run at any time: it only reads, plus one harmless
raw `echo` per cluster that must be refused once the allowlist is in force.
"""

import hashlib
import json

from globus_compute_sdk import ShellFunction

from gcx import allowlist, config, registry, restart
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

    online = restart.status(client, cfg["endpoint"])
    mode = cfg.get("keepalive", {}).get("mode", "?")
    hint = {"on-use": f"; the next gcx call restarts it (needs `gcx login {cluster}`)",
            "none": f"; start it with `gcx setup {cluster}`"}.get(mode, "")
    yield "endpoint online (Globus service)", online == "online", \
        f"{online} (keepalive: {mode})" + ("" if online == "online" else hint)
    if online != "online":
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


def _line(name, passed, detail):
    return f"{'PASS' if passed else 'FAIL'}  {name:42s} {detail}"


def main(client, cluster):
    ok = True
    for name, passed, detail in checks(client, cluster):
        ok &= passed
        print(_line(name, passed, detail))
    return 0 if ok else 1


def collect(client_factory, cluster):
    """All check results for one cluster; an error becomes a failed check."""
    results = []
    try:
        for r in checks(client_factory(), cluster):
            results.append(r)
    except Exception as e:  # network, auth, a cluster gone missing: report, don't crash
        results.append(("doctor finished", False, f"{type(e).__name__}: {str(e)[:120]}"))
    return results


def main_all(client_factory, clusters, verbose=False, workers=8):
    """Check every cluster in parallel; one summary line each."""
    from concurrent.futures import ThreadPoolExecutor
    if not clusters:
        print("[gcx] no clusters configured; set one up with `gcx setup <cluster>`")
        return 1
    # One client per cluster: the SDK client serializes its own requests.
    with ThreadPoolExecutor(max_workers=min(workers, len(clusters))) as pool:
        results = dict(zip(clusters, pool.map(lambda c: collect(client_factory, c), clusters)))
    width = max(map(len, clusters))
    bad = 0
    for c in clusters:
        res = results[c]
        failed = [r for r in res if not r[1]]
        host = next((d.split(",")[0] for n, p, d in res if n == "endpoint answers" and p), "")
        if failed:
            bad += 1
            print(f"FAIL  {c:{width}s}  {len(failed)} of {len(res)} checks failed")
        else:
            print(f"PASS  {c:{width}s}  {len(res)} checks" + (f", endpoint on {host}" if host else ""))
        for r in (res if verbose else failed):
            print("        " + _line(*r))
    print(f"\n{len(clusters) - bad} of {len(clusters)} clusters healthy")
    return 0 if not bad else 1
