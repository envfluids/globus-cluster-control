"""`gcx login globus [--check]`: the Globus CLI login that `gcx status` needs.

gcx itself uses the Globus Compute SDK's login (setup asks once per computer)
and each endpoint's own login. Only `gcx status` uses the Globus CLI: it lists
the endpoint's keepalive state through the cluster's Globus collection. That
needs the CLI logged in and, for most collections, a data_access consent per
collection, which Globus grants per computer.

`--check` never prompts or opens a browser, so anyone may run it. Without it,
the human logs in and consents, pasting codes from URLs (`--no-local-server`,
so it also works over SSH and on WSL, and never waits on a local port).
"""

import os
import subprocess
import sys

from gcx import config, profiles

DATA_ACCESS = "https://auth.globus.org/scopes/{}/data_access"
INSTALL = "uv tool install globus-cli"


def collections():
    """{cluster: (collection id, home path)} for configured clusters that have one."""
    out = {}
    for c in config.configured():
        try:
            prof = profiles.load(c)
        except profiles.UnknownCluster:
            continue
        if prof["transfer_collection"]:
            out[c] = (prof["transfer_collection"], prof["transfer_home"])
    return out


def cli():
    """The Globus CLI's path, or None if it is not installed."""
    path = config.globus_cli()
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _quiet(path, *args):
    try:
        return subprocess.run([path, *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out\n")


def state(path, only=None):
    """(logged in?, {cluster: None if `gcx status` can list it, else "consent" or the error})."""
    if _quiet(path, "whoami").returncode != 0:
        return False, {}
    res = {}
    for c, (uuid, home) in collections().items():
        if only and c != only:
            continue
        p = _quiet(path, "ls", f"{uuid}:{home}")
        text = (p.stderr + p.stdout).strip()
        res[c] = (None if p.returncode == 0 else "consent" if "consent" in text.lower()
                  else (text.splitlines() or [f"exit {p.returncode}"])[-1])
    return True, res


def ready(cluster):
    path = cli()
    if not path:
        return False
    logged_in, res = state(path, only=cluster)
    return logged_in and res.get(cluster) is None


def _report(res):
    for c, problem in sorted(res.items()):
        if problem is None:
            print(f"  ok      {c:10s} `gcx status {c}` works")
        elif problem == "consent":
            print(f"  missing {c:10s} consent for its Globus collection: `gcx login globus`")
        else:
            print(f"  error   {c:10s} {problem}")
    return 0 if all(p is None for p in res.values()) else 1


def _preamble():
    """The CLI's path, or None after saying why nothing needs doing or can be done."""
    if not collections():
        print("[gcx] none of your clusters has a Globus collection for `gcx status`; "
              "nothing in gcx needs the Globus CLI")
        return None
    path = cli()
    if not path:
        print(f"[gcx] the Globus CLI is not installed. Only `gcx status` needs it: "
              f"`{INSTALL}`, then `gcx login globus`")
    return path


def check():
    """Exit 0 if `gcx status` works on every cluster that has a collection."""
    path = _preamble()
    if path is None:
        return 0 if not collections() else 1
    logged_in, res = state(path)
    if not logged_in:
        print("[gcx] the Globus CLI is not logged in: `gcx login globus`")
        return 1
    return _report(res)


def fix():
    """Log the Globus CLI in and grant only the missing collection consents."""
    path = _preamble()
    if path is None:
        return 0 if not collections() else 1
    logged_in, res = state(path)
    if not logged_in:
        print("\nLog the Globus CLI in: open the URL below, sign in, and paste the code back.\n")
        if subprocess.run([path, "login", "--no-local-server"]).returncode:
            return 1
        logged_in, res = state(path)
        if not logged_in:
            print("[gcx] the Globus CLI still reports no login", file=sys.stderr)
            return 1
    need = [c for c, p in sorted(res.items()) if p == "consent"]
    if need:
        cols = collections()
        print(f"\nGrant the Globus CLI access to the Globus collection of {', '.join(need)}: "
              f"open the URL below, sign in, and paste the code back.\n")
        scopes = [DATA_ACCESS.format(cols[c][0]) for c in need]
        if subprocess.run([path, "session", "consent", "--no-local-server", *scopes]).returncode:
            return 1
        _, res = state(path)
    return _report(res)
