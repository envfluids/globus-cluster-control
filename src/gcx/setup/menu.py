"""The questions `gcx setup` asks. Every question has a default; --yes takes them."""

import re
import subprocess

from gcx import profiles

MENU = [
    ("status", "Job status: jobs, history, queues (read-only)", True),
    ("submit", "Submit and cancel jobs: existing scripts in chosen directories, "
               "your own jobs only", True),
    ("read", "Read files and logs: ls, tail, head, du in chosen directories", True),
    ("shell", "Arbitrary shell commands (sh). Overrides every other restriction: "
              "anything holding your Globus tokens could run any command as you", False),
]


class Prompter:
    def __init__(self, assume_yes=False):
        self.yes = assume_yes

    def ask(self, question, default=""):
        if self.yes:
            print(f"  {question} [{default}] -> {default}")
            return default
        got = input(f"  {question} [{default}]: ").strip()
        return got or default

    def confirm(self, question, default=True):
        d = "Y/n" if default else "y/N"
        if self.yes:
            print(f"  {question} [{d}] -> {'yes' if default else 'no'}")
            return default
        got = input(f"  {question} [{d}]: ").strip().lower()
        return default if not got else got.startswith("y")

    def choose_list(self, question, default):
        """Typed values REPLACE the default (accounts, queues: "restrict to these")."""
        while True:
            got = self.ask(question + " (comma-separated)", ", ".join(default))
            items = [] if got.strip().lower() in NONE_WORDS else _split(got)
            if self._accept(items):
                return items

    def extend_list(self, question, default):
        """Typed paths are ADDED to the defaults; "-path" removes a default.

        Replacing on input lost the defaults when someone typed one extra path
        (DSI, 2026-10-01), so directories are always additive.
        """
        print(f"  {question}: {', '.join(default) or '(none)'}")
        while True:
            got = self.ask("Add directories (comma-separated; -PATH removes one, "
                           "e.g. -~; Enter keeps these)", "")
            items = list(default)
            bad = [x for x in _split(got) if not x.startswith("-") and not is_path(x)]
            if bad:
                hint = (" (that looks like an answer to \"Use this?\", which comes next)"
                        if any(b.lower() in YES_NO for b in bad) else "")
                print(f"  not a directory: {', '.join(bad)}{hint}. Use absolute paths, "
                      f"~/... or $VAR/...; Enter keeps the list.")
                continue
            for x in _split(got):
                if x.startswith("-"):
                    if x[1:] not in items:
                        print(f"  ({x[1:]} is not in the list; nothing to remove)")
                    items = [i for i in items if i != x[1:]]
                elif x.rstrip("/") not in (i.rstrip("/") for i in items):
                    items.append(x)
            if self._accept(items):
                return items

    def _accept(self, items):
        if self.yes:
            return True
        return self.confirm(f"-> {', '.join(items) or '(none)'}. Use this?", True)


YES_NO = ("y", "n", "yes", "no")
NONE_WORDS = ("none", "-")


def is_path(x):
    """An absolute, ~ or $VAR path, and not the whole filesystem."""
    return bool(re.fullmatch(r"(/|~|\$)[^\s,]*", x)) and x.rstrip("/") != ""


def _split(text):
    return [x.strip() for x in text.split(",") if x.strip()]


ALWAYS_SET = ("$USER", "$HOME")  # present in the endpoint worker's environment


def resolve_roots(roots, facts):
    """Replace login-shell-only variables ($WORK, ...) with the probed paths.

    The endpoint's worker does not run a login shell, so only $USER and $HOME
    are reliably set there; anything else would silently match nothing.
    """
    out = []
    for r in roots:
        head, sep, rest = r.partition("/")
        if head.startswith("$") and head not in ALWAYS_SET:
            value = facts.get("env_" + head[1:])
            if not value:
                print(f"  (dropping {r}: {head} is not set on this cluster)")
                continue
            r = value + sep + rest
        r = r.rstrip("/") if r != "/" else r
        if r not in out:  # "~/" and "~" are the same root (Polaris, 2026-10-02)
            out.append(r)
    return out


def _csv(facts, key):
    return [x for x in facts.get(key, "").split(",") if x]


def build_policy(cluster, prof, facts, root, ask):
    """Interactive command menu; returns a policy dict for capabilities/build.py."""
    print(f"\nWhich commands should gcx be able to run on {cluster}?")
    caps = []
    for cap, text, default in MENU:
        if cap == "status":
            print(f"  - {text}: always on")
            caps.append(cap)
            continue
        if ask.confirm(f"{text}?", default):
            caps.append(cap)
    if "shell" in caps and not ask.confirm(
            "Shell really on? It makes the other restrictions advisory", False):
        caps.remove("shell")

    policy = {"scheduler": prof["scheduler"], "capabilities": caps}
    test_dir = root + "/test"  # setup's own test job lives here
    if "read" in caps:
        roots = resolve_roots(ask.extend_list("Directories gcx may read under",
                                              prof["read_roots"]), facts)
        policy["read_roots"] = roots + ([test_dir] if not _covers(roots, test_dir) else [])
    if "submit" in caps:
        roots = resolve_roots(ask.extend_list("Directories gcx may submit scripts from",
                                              prof["script_roots"]), facts)
        policy["script_roots"] = roots + ([test_dir] if not _covers(roots, test_dir) else [])
        accounts = [a for a in _csv(facts, "accounts") if a != "default"]  # TACC lists a pseudo-account
        if not accounts and prof.get("accounts_from_groups"):
            # PBS has no account list; NCAR projects are Unix groups (uchi0014).
            accounts = [g for g in _csv(facts, "groups")
                        if re.fullmatch(prof["accounts_from_groups"], g)]
        if prof.get("accounts_upper"):
            accounts = [a.upper() for a in accounts]
        if prof["scheduler"] == "slurm" and accounts:
            print(f"  Your accounts on {cluster}: {', '.join(accounts)}")
        policy["accounts"] = ask.choose_list("Accounts gcx may submit to",
                                             accounts or prof.get("accounts", []))
        q = prof["test_job"].get("queue")
        policy["queues"] = ask.choose_list("Queues/partitions gcx may submit to", [q] if q else [])
        qos = ask.choose_list("QoS values gcx may request (blank for none)", [])
        if qos:
            policy["qos"] = qos
        if prof["submit_extra"]:
            policy["submit_extra"] = list(prof["submit_extra"])
    return policy


def _covers(roots, path):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def choose_keepalive(cluster, prof, facts, ask):
    """Probe, then degrade: failover if named nodes + cron, else single, else none."""
    here = facts.get("host", "")
    named = [profiles.short(n) for n in prof["login_nodes"]]
    if facts.get("cron") != "ok":
        print(f"\n{cluster}: cron is not available ({facts.get('cron')}), so nothing on the "
              f"cluster can restart the endpoint after a login-node reboot.")
        if ask.confirm("Restart it on use instead? (when a gcx call finds it offline and your "
                       f"SSH connection to {cluster} is up, gcx restarts it over SSH)", True):
            return {"mode": "on-use"}
        print(f"  Then re-run `gcx setup {cluster}` whenever `gcx doctor {cluster}` shows it offline.")
        return {"mode": "none"}
    if here in named and len(named) > 1:
        others = [n for n in named if n != here]
        print(f"\n{cluster} has named login nodes, so the endpoint can fail over.")
        if ask.confirm(f"Primary {here}, backup {others[0]}? (you will log in to "
                       f"{others[0]} once, with MFA, to install its cron entry)", True):
            return {"mode": "failover", "primary": here, "backup": others[0]}
    print(f"\n{cluster}: the endpoint will run on {here}, restarted by cron if it dies.")
    return {"mode": "single", "primary": here}


def email_default():
    p = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
    return p.stdout.strip()
