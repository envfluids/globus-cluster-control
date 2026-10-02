"""`gcx setup <cluster>`: ordered, idempotent steps.

Each step looks at the current state (mostly via one probe round trip) and
returns OK, or what it would change and a callable that changes it. A dry run
only prints; a real run applies each change, re-probes, and re-checks the step
before moving on. Re-running setup on a finished cluster changes nothing.

Order matters for safety: the allowlist is written into the endpoint's config
before the endpoint first starts, so it is never briefly unrestricted.
"""

import json
import secrets
import shlex
import sys
import time
from dataclasses import dataclass, field
from importlib import resources

import globus_compute_sdk

from gcx import allowlist, config, doctor, profiles, registry
from gcx.capabilities.build import entry_functions
from gcx.setup import menu
from gcx.setup.remote import Remote, RemoteError

OK, CHANGE, HUMAN, BLOCKED = "ok", "change", "you", "blocked"
STALE_S = 360
SERVICE_WAIT_S = 480


@dataclass
class Result:
    status: str
    detail: str
    action: object = None  # callable() for CHANGE / HUMAN


@dataclass
class Ctx:
    cluster: str
    prof: dict
    cfg: dict
    remote: Remote
    ask: menu.Prompter
    facts: dict = field(default_factory=dict)
    client: object = None
    needs_restart: bool = False

    # --- layout -----------------------------------------------------------
    @property
    def root(self):
        return self.cfg.get("remote_root", "~/.gcx")

    @property
    def ep(self):
        return self.cfg.get("endpoint_name", "gcx")

    @property
    def gce(self):
        return self.remote.abs(self.root) + "/venv/bin/globus-compute-endpoint"

    @property
    def epdir(self):
        return f"~/.globus_compute/{self.ep}"

    def save(self):
        config.save(self.cluster, self.cfg)

    def probe(self):
        self.facts = self.remote.probe(self.root, self.ep)

    def get_client(self):
        if self.client is None:
            from gcx.cli import _client
            self.client = _client()
        return self.client


def sdk_version():
    return globus_compute_sdk.__version__


# --- steps ------------------------------------------------------------------

def s_ssh(c):
    if c.remote.reachable():
        return Result(OK, f"ssh {c.remote.alias}")
    return Result(BLOCKED, f"ssh {c.remote.alias} failed. If the alias is new: "
                           f"`gcx ssh-config {c.cluster} --apply`. Then log in (you answer MFA): "
                           f"`gcx login {c.cluster}`, and re-run setup")


def s_probe(c):
    c.probe()
    f = c.facts
    if f.get("scheduler") != c.prof["scheduler"]:
        return Result(BLOCKED, f"expected {c.prof['scheduler']}, found {f.get('scheduler')}")
    if f.get("net_api_443") != "ok" or "ok" not in (f.get("net_amqps_5671"), f.get("net_amqps_443")):
        return Result(BLOCKED, "login node cannot reach Globus Compute (compute.api / "
                               "compute.amqps.globus.org); an endpoint cannot run here")
    return Result(OK, f"{f['host']}: {f['scheduler']}, cron {f.get('cron')}, "
                      f"Python {f.get('python3') or 'none'}, uv {'yes' if f.get('uv') else 'no'}")


def s_venv(c):
    want, have = sdk_version(), c.facts.get("gce_version")
    if have == want:
        return Result(OK, f"globus-compute-endpoint {have} in {c.root}/venv")
    venv = shlex.quote(c.remote.abs(c.root) + "/venv")
    env = " ".join(f"export {k}={shlex.quote(str(v))};" for k, v in c.prof["uv_env"].items())
    cmd = (f'set -e; export PATH="$HOME/.local/bin:$PATH"; {env} '
           # UV_NO_MODIFY_PATH: never edit the user's shell startup files.
           "command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | "
           "env UV_NO_MODIFY_PATH=1 sh; "
           f"uv venv -q --allow-existing --python 3.12 {venv}; "
           f"VIRTUAL_ENV={venv} uv pip install -q globus-compute-endpoint=={want}")
    return Result(CHANGE, f"install globus-compute-endpoint {want} in {c.root}/venv "
                          f"(have: {have or 'none'}; uv, Python 3.12)",
                  lambda: c.remote.run(cmd, timeout=1800))


def s_tokens(c):
    if c.facts.get("tokens") == "yes":
        return Result(OK, "endpoint has Globus tokens")
    cmd = f"{c.gce} login"

    def act():
        print(f"\n  Log in to Globus for the {c.cluster} endpoint: open the URL below, "
              f"sign in, and paste the code back here.\n")
        if c.remote.interactive(cmd):
            raise RemoteError("endpoint login did not complete")
    return Result(HUMAN, f"endpoint Globus login (browser + paste a code): ssh -t "
                         f"{c.remote.alias} {shlex.quote(cmd)}", act)


def s_configure(c):
    if c.facts.get("ep_configured") == "yes":
        return Result(OK, f"endpoint {c.ep} configured")
    name = shlex.quote(f"gcx {c.cluster} ({c.facts.get('user')})")
    cmd = f"cd ~ && {c.gce} configure --multi-user false --display-name {name} {c.ep}"
    return Result(CHANGE, f"configure endpoint {c.ep}", lambda: c.remote.run(cmd))


def endpoint_files(c):
    data = resources.files("gcx.data").joinpath("endpoint")
    # ~/.local/bin keeps the user's own tools (uv, ...) on PATH for `gcx <c> sh`.
    path = (f"{c.remote.abs(c.root)}/venv/bin:{c.remote.abs('~/.local/bin')}:"
            f"{c.facts.get('sched_bin')}:/usr/local/bin:/usr/bin:/bin")
    return {
        f"{c.epdir}/user_config_template.yaml.j2": data.joinpath(
            "user_config_template.yaml.j2").read_text().replace(
            "@MAX_WORKERS@", str(c.prof["max_workers"])),
        f"{c.epdir}/user_config_schema.json": data.joinpath("user_config_schema.json").read_text(),
        f"{c.epdir}/user_environment.yaml": f"# Written by gcx setup.\nPATH: {path}\n",
    }


def _differing(c, files):
    return {p: t for p, t in files.items() if c.remote.read(p) != t}


def s_endpoint_files(c):
    diff = _differing(c, endpoint_files(c))
    if not diff:
        return Result(OK, "endpoint template, schema and environment current")

    def act():
        for p, t in diff.items():
            c.remote.write(p, t)
        if c.facts.get("ep_running") == "yes" or _owner_fresh(c):
            c.needs_restart = True
    return Result(CHANGE, "write " + ", ".join(p.rsplit("/", 1)[1] for p in diff), act)


def s_policy(c):
    if c.cfg.get("policy"):
        return Result(OK, "commands: " + ", ".join(c.cfg["policy"]["capabilities"]))

    def act():
        c.cfg["policy"] = menu.build_policy(c.cluster, c.prof, c.facts, c.root, c.ask)
        c.save()
    return Result(HUMAN, "choose which commands gcx may run", act)


def s_register(c):
    if c.cfg.get("policy") and registry.current(c.cfg):
        return Result(OK, f"{len(c.cfg['functions'])} functions registered")

    def act():
        c.save()
        registry.register(c.get_client(), c.cluster)
        c.cfg = config.load(c.cluster)
    n = len(entry_functions(c.cfg["policy"])) if c.cfg.get("policy") else "?"
    return Result(CHANGE, f"register {n} functions for the policy", act)


def _wanted(c):
    funcs = registry.current(c.cfg) or {}
    return {f["uuid"] for f in funcs.values()}


def s_config_yaml(c):
    path = f"{c.epdir}/config.yaml"
    cur = c.remote.read(path) or ""
    new = allowlist.render(cur, _wanted(c))
    if cur == new:
        return Result(OK, f"config.yaml allows exactly {len(_wanted(c))} functions")

    def act():
        c.remote.write(path, new)
        if c.facts.get("ep_running") == "yes" or _owner_fresh(c):
            c.needs_restart = True
    return Result(CHANGE, "write the function allowlist into config.yaml", act)


def _owner_fresh(c):
    age = c.facts.get("owner_age")
    return bool(c.facts.get("owner")) and age not in (None, "") and int(age) < STALE_S


def s_running(c):
    if c.facts.get("ep_running") == "yes":
        return Result(OK, f"endpoint running on {c.facts['host']}")
    if _owner_fresh(c):
        return Result(OK, f"endpoint held by {c.facts['owner']} (keepalive lease "
                          f"{c.facts['owner_age']} s old)")
    # Claim the keepalive lease too: on a failover cluster, the other node's cron
    # must see this node as the holder, or it would start a second endpoint.
    st = c.remote.q(c.root + "/state")
    cmd = (f"cd ~ && rm -f {c.epdir}/daemon.pid && {c.gce} start --detach {c.ep} && sleep 10 && "
           f"mkdir -p {st} && hostname -s > {st}/owner && touch {st}/owner-is.$(hostname -s)")
    return Result(CHANGE, f"start the endpoint on {c.facts.get('host')}",
                  lambda: c.remote.run(cmd, timeout=180))


def personal(c):
    alias = c.remote.alias
    root = c.root
    return {
        "ssh": alias,
        "remote_root": root,
        "endpoint_name": c.ep,
        "remote_state": root + "/state",
        "endpoint": c.facts.get("ep_id") or c.cfg.get("endpoint"),
        "state": f"{c.prof['transfer_collection']}:{c.prof['transfer_home']}"
                 f"{root[2:]}/state/" if c.prof["transfer_collection"] else None,
    }


def s_personal(c):
    want = personal(c)
    if not want["endpoint"]:
        return Result(BLOCKED, "endpoint has no ID yet (it registers on first start)")
    stale = {k: v for k, v in want.items() if c.cfg.get(k) != v}
    if not stale:
        return Result(OK, f"endpoint {want['endpoint']}")

    def act():
        c.cfg.update(stale)
        c.save()
    return Result(CHANGE, "record " + ", ".join(sorted(stale)) + " in your gcx config", act)


def s_keepalive_mode(c):
    ka = c.cfg.get("keepalive")
    if ka:
        nodes = [ka.get("primary"), ka.get("backup")]
        return Result(OK, f"{ka['mode']}" + (f": {', '.join(n for n in nodes if n)}"
                                             if ka["mode"] != "none" else ""))

    def act():
        c.cfg["keepalive"] = menu.choose_keepalive(c.cluster, c.prof, c.facts, c.ask)
        if c.cfg["keepalive"]["mode"] != "none" and "email" not in c.cfg:
            c.cfg["email"] = c.ask.ask("Email for keepalive alerts (cron mail; blank for none)",
                                       menu.email_default())
        c.save()
    return Result(HUMAN, "choose how the endpoint is kept alive", act)


def keepalive_files(c):
    ka = c.cfg["keepalive"]
    root = c.remote.abs(c.root)
    env = (f"# Written by gcx setup.\nMODE={ka['mode']}\nPRIMARY={ka['primary']}\n"
           f"BACKUP={ka.get('backup', '')}\nEP={c.ep}\nGCE={c.gce}\nSTATE={root}/state\n"
           f"STALE={STALE_S}\n")
    script = resources.files("gcx.data").joinpath("keepalive.sh").read_text()
    # Settings first: a cron tick between the two writes then finds a complete pair.
    return {f"{c.root}/keepalive.env": env, f"{c.root}/keepalive.sh": script}


CRON_MODES = ("failover", "single")


def s_keepalive_files(c):
    if c.cfg["keepalive"]["mode"] not in CRON_MODES:
        return Result(OK, "no cron keepalive")
    diff = _differing(c, keepalive_files(c))
    if not diff:
        return Result(OK, "keepalive script and settings current")

    def act():
        for p, t in diff.items():
            c.remote.write(p, t, mode="755" if p.endswith(".sh") else None)
    return Result(CHANGE, "write " + ", ".join(p.rsplit("/", 1)[1] for p in diff), act)


def _heartbeats(c):
    out = {}
    for item in filter(None, c.facts.get("heartbeats", "").split(",")):
        node, _, age = item.rpartition(":")
        out[node] = int(age)
    return out


def _cron_cmd(c, role):
    line = f"*/2 * * * * {c.remote.abs(c.root)}/keepalive.sh {role} # gcx-keepalive {c.cluster}"
    mail = c.cfg.get("email", "")
    keep = f"crontab -l 2>/dev/null | grep -v 'gcx-keepalive {c.cluster}$'"
    add_mail = (f"crontab -l 2>/dev/null | grep -q '^MAILTO=' || echo MAILTO={shlex.quote(mail)}; "
                if mail else "")
    return f"( {keep}; {add_mail}echo {shlex.quote(line)} ) | crontab -"


def _alias_resolves(alias, fqdn):
    import subprocess
    p = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    return p.returncode == 0 and f"\nhostname {fqdn}\n" in "\n" + p.stdout


def s_cron(c):
    ka = c.cfg["keepalive"]
    if ka["mode"] == "on-use":
        return Result(OK, "no cron here; gcx restarts the endpoint over SSH when a call finds it offline")
    if ka["mode"] == "none":
        return Result(OK, f"not supervised: re-run `gcx setup {c.cluster}` if the endpoint stops")
    beats = _heartbeats(c)
    roles = [("primary" if ka["mode"] == "failover" else "single", ka["primary"])]
    if ka["mode"] == "failover":
        roles.append(("backup", ka["backup"]))
    missing = [(r, n) for r, n in roles if beats.get(n, 10**9) >= STALE_S]
    if not missing:
        return Result(OK, "cron ticking on " + ", ".join(f"{n} ({beats[n]} s ago)" for _, n in roles))
    here = c.facts.get("host")
    role, node = missing[0]
    cmd = _cron_cmd(c, role)
    if node == here:
        return Result(CHANGE, f"install the {role} cron entry on {node}",
                      lambda: (c.remote.run(cmd), c.remote.run(f"{c.remote.abs(c.root)}/keepalive.sh {role}")))
    fqdn = next((n for n in c.prof["login_nodes"] if profiles.short(n) == node), node)
    # Prefer the per-node alias from `gcx ssh-config`: it gets its own reusable master.
    host = node if _alias_resolves(node, fqdn) else f"{c.facts.get('user')}@{fqdn}"

    def act():
        print(f"\n  Installing the {role} cron entry needs a login to {fqdn} itself "
              f"(MFA). Running: ssh -t {host} ...")
        if c.remote.interactive(f"{cmd} && {c.remote.abs(c.root)}/keepalive.sh {role}; "
                                f"crontab -l | tail -2", host=host):
            raise RemoteError(f"could not install cron on {fqdn}")
    return Result(HUMAN, f"install the {role} cron entry on {node} (login to that node, MFA)", act)


def s_service(c):
    want = _wanted(c)
    restricted, live = allowlist.service_state(c.get_client(), c.cfg["endpoint"])
    pending = c.needs_restart or c.facts.get("restart_pending") == "yes"
    if restricted and live == want and not pending:
        return Result(OK, f"Globus enforces the allowlist ({len(live)} functions)")
    why = "restart to apply new endpoint files" if restricted and live == want else \
        "endpoint must restart so Globus enforces the allowlist"

    def act():
        if c.cfg.get("keepalive", {}).get("mode", "none") in CRON_MODES:
            c.remote.run(f"touch {c.remote.q(c.root + '/state/restart-request')}")
            print("  restart requested; the keepalive picks it up within 2 minutes")
        else:
            c.remote.run(f"cd ~ && {c.gce} stop {c.ep}; sleep 5; rm -f {c.epdir}/daemon.pid; "
                         f"{c.gce} start --detach {c.ep} && sleep 10", timeout=180)
        deadline = time.time() + SERVICE_WAIT_S
        while time.time() < deadline:
            time.sleep(15)
            r, l = allowlist.service_state(c.get_client(), c.cfg["endpoint"])
            if r and l == want and c.remote.probe(c.root, c.ep).get("restart_pending") != "yes":
                c.needs_restart = False
                return
        raise RemoteError("timed out waiting for the endpoint to restart")
    return Result(CHANGE, why, act)


STEPS = [
    ("ssh", s_ssh), ("probe", s_probe), ("venv", s_venv), ("tokens", s_tokens),
    ("configure", s_configure), ("endpoint-files", s_endpoint_files), ("policy", s_policy),
    ("register", s_register), ("allowlist", s_config_yaml), ("running", s_running),
    ("config", s_personal), ("keepalive", s_keepalive_mode),
    ("keepalive-files", s_keepalive_files), ("cron", s_cron), ("enforced", s_service),
]
# Steps whose check needs the facts refreshed after an earlier step changed something.
NO_REPROBE = {"ssh", "probe", "policy", "register", "config", "keepalive"}


def run(cluster, dry_run=False, assume_yes=False, test_job=None):
    prof = profiles.load(cluster)
    try:
        cfg = config.load(cluster)
    except config.NotConfigured:
        cfg = {}
    c = Ctx(cluster, prof, cfg, Remote(cfg.get("ssh", cluster)), menu.Prompter(assume_yes))
    print(f"gcx setup {cluster}{' (dry run)' if dry_run else ''}")
    pending = 0
    for name, step in STEPS:
        res = step(c)
        print(f"  {res.status:7s} {name:16s} {res.detail}")
        if res.status == BLOCKED:
            print(f"\n{cluster}: blocked at `{name}`; fix that and re-run `gcx setup {cluster}`.")
            return 1
        if res.status == OK:
            continue
        pending += 1
        if res.status == HUMAN and not dry_run and not assume_yes and not sys.stdin.isatty():
            # Prompts and pasted codes need a person at a terminal.
            print(f"\n{cluster}: the next step needs you at a terminal. "
                  f"Run `gcx setup {cluster}` yourself to continue from here.")
            return 3
        if dry_run:
            if name in ("ssh", "probe", "venv", "tokens", "configure", "policy", "running"):
                print(f"\n{cluster}: later steps depend on `{name}`; stopping the dry run here.")
                return 2
            continue
        # A step may need several rounds (cron: primary node, then backup node).
        for _ in range(3):
            res.action()
            if name not in NO_REPROBE:
                c.probe()
            again = step(c)
            if again.status == OK or again.detail == res.detail:
                break
            print(f"  {again.status:7s} {name:16s} {again.detail}")
            res = again
        if again.status != OK:
            print(f"\n{cluster}: `{name}` did not take effect; see above.")
            return 1
        print(f"  {'done':7s} {name:16s} {again.detail}")
    if dry_run:
        print(f"\n{cluster}: " + ("nothing to change." if not pending else
                                  f"{pending} step(s) would change; run without --dry-run."))
        return 0 if not pending else 2
    print()
    rc = doctor.main(c.get_client(), cluster)
    if rc == 0 and test_job is not False:
        rc = run_test_job(c, ask_first=test_job is None)
    return rc


def run_test_job(c, ask_first=True):
    """Submit, follow and read back a 1-CPU job through gcx itself."""
    pol = c.cfg["policy"]
    if "submit" not in pol["capabilities"]:
        print(f"\n(skipping the test job: submit is not enabled on {c.cluster})")
        return 0
    acct = pol.get("accounts", [None])[0] if pol.get("accounts") else None
    queue = c.prof["test_job"].get("queue") or (pol.get("queues") or [None])[0]
    if ask_first:
        res = " ".join(c.prof["test_job"].get("resources", [])) or "scheduler defaults"
        print(f"\nTest job (~1 minute) on {c.cluster}: {res}"
              + (f", account {acct}" if acct else "") + (f", queue {queue}" if queue else "") + ".")
        acct = c.ask.ask("Account", acct or "") or None
        queue = c.ask.ask("Queue", queue or "") or None
        if not c.ask.confirm("Submit it now?", True):
            return 0
    token = secrets.token_hex(4)
    sched = c.prof["scheduler"]
    directive = "#SBATCH" if sched == "slurm" else "#PBS"
    res = c.prof["test_job"].get("resources", [])
    script = "#!/bin/bash\n" + "".join(f"{directive} {r}\n" for r in res) + \
        f'echo "gcx-test-ok {token} on $(hostname -s)"\n'
    path = f"{c.root}/test/hello.{sched}"
    c.remote.write(path, script)
    from gcx.cli import call_capability
    sub = call_capability(c.cluster, "gcx_submit", script=path, account=acct, queue=queue,
                          walltime="00:05:00", job_name="gcx-setup-test")
    if sub.get("rc") != 0 or not sub.get("job_id"):
        print(f"FAIL  test job submission: {sub.get('stderr', '').strip()[-300:]}")
        return 1
    job = sub["job_id"]
    print(f"      submitted {job}; waiting for it to finish")
    deadline = time.time() + 1800
    while time.time() < deadline:
        time.sleep(20)
        h = call_capability(c.cluster, "gcx_history", job_ids=[job])
        text = h.get("stdout", "")
        if any(s in text for s in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", " F ")):
            break
    out_name = f"slurm-{job}.out" if sched == "slurm" else None
    if out_name and "read" in pol["capabilities"]:
        r = call_capability(c.cluster, "gcx_read", path=f"{c.root}/test/{out_name}", mode="tail", lines=5)
        ok = f"gcx-test-ok {token}" in r.get("stdout", "")
        print(f"{'PASS' if ok else 'FAIL'}  test job {job} output {'checked' if ok else 'missing'}"
              f"{'' if ok else ': ' + r.get('stdout', '')[-200:] + r.get('stderr', '')[-200:]}")
        return 0 if ok else 1
    ok = "COMPLETED" in text or " F " in text
    print(f"{'PASS' if ok else 'FAIL'}  test job {job} finished")
    return 0 if ok else 1
