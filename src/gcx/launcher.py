"""`gcx launcher add|remove|list`: change a cluster's `run` launchers without the menu.

A launcher is part of the cluster's policy, and the policy is built into the
code registered on the endpoint. So add and remove re-apply it the way
`gcx setup` does: register the functions, rewrite the allowlist, restart the
endpoint, run doctor and rewrite the skill. That needs your SSH connection, so
they are for the human, not agents. `--dry-run` only checks and prints, and
`list` only reads the laptop config; both are safe for anyone.
"""

import argparse
import copy
import re
import shlex
import sys

from gcx import config
from gcx.capabilities import build
from gcx.setup import menu

DEFAULT_TIMEOUT = 120

USAGE = """gcx launcher add CLUSTER ROOT [--timeout S] [--allow-risky] [--replace] [--dry-run] [-- COMMAND ...]
gcx launcher remove CLUSTER ROOT [--dry-run]
gcx launcher list CLUSTER

A launcher lets `gcx CLUSTER run` submit scripts under ROOT. COMMAND runs from
ROOT with the script's path appended and prints the batch script to submit;
the default, cat, submits the script as written. add and remove re-register
the cluster's functions and restart its endpoint (about a minute), so they
need `gcx login CLUSTER` first. `run` itself must already be on: that is
`gcx setup CLUSTER --menu`, which also chooses its accounts and queues."""


class LauncherError(Exception):
    pass


def _root(root):
    """The launcher root as the policy stores it: ~ for the laptop's own home
    (an unquoted ~ the shell expanded), no trailing slash."""
    from gcx.cli import _cluster_path
    root = _cluster_path(root)
    if not menu.is_path(root):
        raise LauncherError(f"{root}: give an absolute, ~/... or $USER/$HOME path, not /")
    head = root.split("/", 1)[0]
    if head.startswith("$") and head not in menu.ALWAYS_SET:
        raise LauncherError(f"{root}: {head} is not set in the endpoint's environment "
                            f"(only {', '.join(menu.ALWAYS_SET)} are); give the absolute path")
    return root.rstrip("/")


def _describe(launcher):
    extra = "" if launcher.get("block_risky", True) else ", risky header options allowed"
    return (f"{launcher['root']}: {shlex.join(launcher['argv'])} "
            f"({launcher.get('timeout', DEFAULT_TIMEOUT)} s{extra})")


def updated_policy(cluster, cfg, action, root, argv=None, timeout=DEFAULT_TIMEOUT,
                   allow_risky=False, replace=False):
    """The cluster's policy with one launcher added or removed, validated."""
    pol = copy.deepcopy(cfg.get("policy") or {})
    if "run" not in pol.get("capabilities", []):
        raise LauncherError(f"run is not enabled on {cluster}. Turn it on with "
                            f"`gcx setup {cluster} --menu`, which also asks which accounts "
                            f"and queues run may use")
    launchers = pol.setdefault("launchers", [])
    same = [l for l in launchers if l["root"].rstrip("/") == root]
    if action == "add":
        if same and not replace:
            raise LauncherError(f"{cluster} already has a launcher for {_describe(same[0])}; "
                                f"pass --replace to change it")
        new = {"root": root, "argv": argv, "timeout": timeout, "block_risky": not allow_risky}
        pol["launchers"] = [l for l in launchers if l not in same] + [new]
    else:
        if not same:
            raise LauncherError(f"{cluster} has no launcher for {root} "
                                f"(see `gcx launcher list {cluster}`)")
        if len(same) == len(launchers):
            raise LauncherError(f"{root} is the only launcher on {cluster}; to turn run off, "
                                f"use `gcx setup {cluster} --menu`")
        pol["launchers"] = [l for l in launchers if l not in same]
    try:
        build.validate(pol)
    except build.PolicyError as e:
        raise LauncherError(str(e))
    return pol


def apply(cluster, cfg, pol):
    """Save the policy and apply it with setup's own steps (no test job)."""
    from gcx.setup import steps
    from gcx.setup.remote import Remote
    alias = cfg.get("ssh", cluster)
    if not Remote(alias).reachable():
        raise LauncherError(f"ssh {alias} is not connected; run `gcx login {cluster}` first "
                            f"(nothing was changed)")
    cfg["policy"] = pol
    config.save(cluster, cfg)
    print(f"[gcx] saved; applying it to {cluster} (register, allowlist, restart)\n")
    rc = steps.run(cluster, test_job=False, offers=False)
    if rc:
        print(f"\n[gcx] the change is saved but applying it stopped above; fix that step "
              f"and run `gcx setup {cluster}`", file=sys.stderr)
    return rc


def list_launchers(cluster):
    pol = config.load(cluster).get("policy") or {}
    if "run" not in pol.get("capabilities", []):
        print(f"run is not enabled on {cluster} (`gcx setup {cluster} --menu` turns it on)")
        return 0
    for launcher in pol.get("launchers", []):
        print(_describe(launcher))
    return 0


def main(argv):
    # Everything after `--` is the launcher command, verbatim: split it off before
    # argparse, which would otherwise read the command's own options as ours.
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="gcx launcher", usage=USAGE)
    sub = p.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", usage=USAGE)
    add.add_argument("cluster")
    add.add_argument("root")
    add.add_argument("--timeout", default=str(DEFAULT_TIMEOUT),
                     help=f"seconds the launcher may take (default {DEFAULT_TIMEOUT})")
    add.add_argument("--allow-risky", action="store_true",
                     help="allow --wrap, --export, --uid/--gid, --get-user-env in rendered headers")
    add.add_argument("--replace", action="store_true", help="change an existing launcher for ROOT")
    add.add_argument("--dry-run", action="store_true", help="check and print; change nothing")
    rm = sub.add_parser("remove", usage=USAGE)
    rm.add_argument("cluster")
    rm.add_argument("root")
    rm.add_argument("--dry-run", action="store_true", help="check and print; change nothing")
    ls = sub.add_parser("list", usage=USAGE)
    ls.add_argument("cluster")
    a = p.parse_args(argv)
    if command and a.action != "add":
        p.error("only add takes a command after --")
    try:
        if a.action == "list":
            return list_launchers(a.cluster)
        cfg = config.load(a.cluster)
        root = _root(a.root)
        if a.action == "add":
            if not re.fullmatch(r"[1-9]\d{0,3}", a.timeout):
                raise LauncherError(f"--timeout: not a number of seconds (1-9999): {a.timeout}")
            pol = updated_policy(a.cluster, cfg, "add", root, command or ["cat"],
                                 int(a.timeout), a.allow_risky, a.replace)
            change = "add " + _describe(pol["launchers"][-1])
        else:
            pol = updated_policy(a.cluster, cfg, "remove", root)
            change = f"remove the launcher for {root}"
        if a.dry_run:
            print(f"[gcx] dry run: would {change} on {a.cluster}, then re-register and restart "
                  f"its endpoint. Launchers afterwards:")
            for launcher in pol["launchers"]:
                print("  " + _describe(launcher))
            return 0
        print(f"[gcx] {change} on {a.cluster}")
        return apply(a.cluster, cfg, pol)
    except LauncherError as e:
        sys.exit(f"[gcx] {e}")
