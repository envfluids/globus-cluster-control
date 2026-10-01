"""The questions `gcx setup` asks. Every question has a default; --yes takes them."""

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
        got = self.ask(question + " (comma-separated)", ", ".join(default))
        return [x.strip() for x in got.split(",") if x.strip()]


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
        roots = ask.choose_list("Directories gcx may read under", prof["read_roots"])
        policy["read_roots"] = roots + ([test_dir] if not _covers(roots, test_dir) else [])
    if "submit" in caps:
        roots = ask.choose_list("Directories gcx may submit scripts from", prof["script_roots"])
        policy["script_roots"] = roots + ([test_dir] if not _covers(roots, test_dir) else [])
        accounts = _csv(facts, "accounts")
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
        print(f"\n{cluster}: cron is not available ({facts.get('cron')}), so nothing will "
              f"restart the endpoint if its login node reboots. `gcx status {cluster}` will "
              f"show it; re-run `gcx setup {cluster}` to start it again.")
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
