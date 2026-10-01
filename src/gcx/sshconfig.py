"""`gcx ssh-config`: SSH aliases with shared ControlMaster sockets, from profiles.

One authenticated connection per cluster (you do the MFA once, in `gcx login`)
is reused by every later ssh -- including `gcx setup` -- without prompting.

The aliases go between "# >>> gcx >>>" fences in ~/.ssh/config. Re-running
replaces only that block. Rules carried over from hpc-agent-toolkit:
- the block goes ABOVE any `Host *`: ssh takes the first value it sees for each
  keyword, and a `Host *` above would override User;
- an alias the user (or another tool) already defines is left alone, never
  shadowed or duplicated.
Failover clusters also get one alias per named login node (midway3-login3 ...)
so setup can reach that node directly to install its cron entry.
"""

import datetime
import difflib
import getpass
import os
import re
import shutil
from pathlib import Path

from gcx import profiles

BEGIN, END = "# >>> gcx >>>", "# <<< gcx <<<"
CONTROL_DIR = "~/.ssh/controlmasters"


def host_block(alias, hostname, user, persist, comment=""):
    lines = [f"Host {alias}"]
    if comment:
        lines.append(f"  # {comment}")
    lines += [
        f"  HostName {hostname}",
        f"  User {user}",
        "  ControlMaster auto",
        f"  ControlPath {CONTROL_DIR}/%r@%h:%p",  # identical everywhere, or two masters
        f"  ControlPersist {persist}",
        "  ServerAliveInterval 60",
        "  ServerAliveCountMax 3",
    ]
    return "\n".join(lines) + "\n"


def entries(cluster, user):
    """(alias, block) pairs for one cluster: its alias, plus its named nodes."""
    prof = profiles.load(cluster)
    persist = prof["ssh_persist"]
    out = [(cluster, host_block(cluster, prof["ssh_host"], user, persist,
                                prof["description"] + (" -- single-use MFA: never close this "
                                                       "master casually" if prof["mfa_single_use"]
                                                       else "")))]
    for node in prof["login_nodes"]:
        short = profiles.short(node)
        out.append((short, host_block(short, node, user, persist,
                                      f"one {cluster} login node, for keepalive failover")))
    return out


def unfence(text):
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)


def defined_hosts(text):
    """Literal Host names defined in text (wildcard patterns ignored)."""
    names = set()
    for m in re.finditer(r"^\s*Host\s+(.+)$", text, re.M | re.I):
        names |= {h for h in m.group(1).split() if not any(ch in h for ch in "*?!")}
    return names


def fenced_clusters(text):
    """{cluster: user} for the cluster aliases currently inside our fence."""
    m = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), text, re.S)
    known = set(profiles.available())
    out = {}
    for block in re.split(r"(?m)^(?=Host\s)", m.group(1) if m else ""):
        h = re.match(r"Host\s+(\S+)", block)
        u = re.search(r"^\s*User\s+(\S+)", block, re.M)
        if h and u and h.group(1) in known:
            out[h.group(1)] = u.group(1)
    return out


def render(current, cluster_users):
    """New config text, and the aliases skipped because they already exist.

    Clusters already in the fence are kept, so adding one never drops another.
    """
    merged = fenced_clusters(current)      # existing order first, new ones appended
    merged.update(cluster_users)
    cluster_users = list(merged.items())
    outside = unfence(current)
    taken = defined_hosts(outside)
    blocks, skipped = [], []
    for cluster, user in cluster_users:
        for alias, block in entries(cluster, user):
            (skipped if alias in taken else blocks).append(alias if alias in taken else block)
    if not blocks:
        return outside if outside.strip() else "", skipped
    fenced = BEGIN + "\n" + "\n".join(blocks) + END + "\n"
    star = re.search(r"^Host\s+\*\s*$", outside, re.M)
    if star:
        new = outside[:star.start()].rstrip("\n") + "\n\n" + fenced + "\n" + outside[star.start():]
    else:
        new = (outside.rstrip("\n") + "\n\n" if outside.strip() else "") + fenced
    return re.sub(r"\n{3,}", "\n\n", new).lstrip("\n"), skipped


def run(cluster_users, path=None, apply=False):
    path = Path(path or Path.home() / ".ssh" / "config")
    current = path.read_text() if path.exists() else ""
    new, skipped = render(current, cluster_users)
    for alias in skipped:
        print(f"[gcx] `Host {alias}` is already defined in {path}; leaving yours in place")
    diff = "".join(difflib.unified_diff(current.splitlines(True), new.splitlines(True),
                                        str(path), "with gcx aliases"))
    if not diff:
        print(f"{path}: up to date")
        return 0
    print(diff, end="")
    if not apply:
        print(f"[gcx] dry run; rerun with --apply to write {path}")
        return 0
    path.parent.mkdir(mode=0o700, exist_ok=True)
    Path(os.path.expanduser(CONTROL_DIR)).mkdir(mode=0o700, parents=True, exist_ok=True)
    if current:
        bak = path.with_name(path.name + ".bak." + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, bak)
        print(f"[gcx] backed up to {bak}")
    path.write_text(new)
    path.chmod(0o600)
    print(f"[gcx] wrote {path}")
    return 0


def default_user():
    return getpass.getuser()
