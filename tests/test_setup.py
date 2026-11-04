"""Setup's decisions for clusters Midway3 cannot exercise: fresh installs,
no cron, single nodes, PBS. No SSH: a stub Remote with a known home."""

import pytest

from gcx import profiles
from gcx.capabilities import build
from gcx.setup import menu, steps
from gcx.setup.remote import Remote, RemoteError

FACTS = {"host": "midway3-login4", "user": "alice", "home": "/home/alice", "cron": "ok",
         "scheduler": "slurm", "sched_bin": "/opt/slurm/bin",
         "accounts": "pi-a,pi-b", "net_api_443": "ok", "net_amqps_5671": "ok"}


def ctx(cfg=None, facts=None, prof=None, yes=True):
    c = steps.Ctx("midway3", prof or profiles.load("midway3"), dict(cfg or {}),
                  Remote("midway3", home="/home/alice"), menu.Prompter(yes))
    c.facts = dict(FACTS, **(facts or {}))
    return c


def test_profile_loads_and_lists():
    p = profiles.load("midway3")
    assert p["scheduler"] == "slurm" and "midway3" in profiles.available()
    with pytest.raises(profiles.UnknownCluster):
        profiles.load("nowhere")


def test_fresh_install_defaults_to_dot_gcx():
    c = ctx()
    assert (c.root, c.ep) == ("~/.gcx", "gcx")
    assert c.gce == "/home/alice/.gcx/venv/bin/globus-compute-endpoint"


def test_yes_policy_defaults_keep_shell_off_and_allow_test_dir(capsys):
    c = ctx()
    pol = menu.build_policy("midway3", c.prof, c.facts, c.root, c.ask)
    assert pol["capabilities"] == ["status", "submit", "read"]
    assert pol["accounts"] == ["pi-a", "pi-b"] and pol["queues"] == ["caslake"]
    assert "~/.gcx/test" not in pol["read_roots"]  # already covered by ~
    build.validate(pol)


def test_test_dir_added_when_roots_do_not_cover_it():
    prof = dict(profiles.load("midway3"), read_roots=["/scratch/x"], script_roots=["/scratch/x"])
    c = ctx(prof=prof)
    pol = menu.build_policy("midway3", prof, c.facts, c.root, c.ask)
    assert pol["read_roots"] == ["/scratch/x", "~/.gcx/test"]
    assert pol["script_roots"] == ["/scratch/x", "~/.gcx/test"]


@pytest.mark.parametrize("facts, mode", [
    ({"cron": "denied"}, "on-use"),
    ({"host": "midway3-login2"}, "single"),      # not a named login node
    ({}, "failover"),                            # named node, cron ok
])
def test_keepalive_probe_then_degrade(facts, mode):
    c = ctx(facts=facts)
    ka = menu.choose_keepalive("midway3", c.prof, c.facts, c.ask)
    assert ka["mode"] == mode
    if mode == "failover":
        assert ka["primary"] == "midway3-login4" and ka["backup"] != "midway3-login4"


def test_single_node_profile_never_offers_failover():
    prof = dict(profiles.load("midway3"), login_nodes=[])
    c = ctx(prof=prof)
    assert menu.choose_keepalive("midway3", prof, c.facts, c.ask) == \
        {"mode": "single", "primary": "midway3-login4"}


def test_keepalive_env_and_cron_line():
    c = ctx({"keepalive": {"mode": "failover", "primary": "n4", "backup": "n3"},
             "email": "a@b.edu"})
    files = steps.keepalive_files(c)
    assert list(files) == ["~/.gcx/keepalive.env", "~/.gcx/keepalive.sh"]  # settings first
    env = files["~/.gcx/keepalive.env"]
    assert "MODE=failover\nPRIMARY=n4\nBACKUP=n3\nEP=gcx\n" in env
    assert "STATE=/home/alice/.gcx/state\n" in env
    cmd = steps._cron_cmd(c, "backup")
    assert "/home/alice/.gcx/keepalive.sh backup # gcx-keepalive midway3 gcx" in cmd
    assert "MAILTO=a@b.edu" in cmd


def test_cron_cmd_replaces_only_this_endpoints_entry(tmp_path):
    # Two installs on one account (here: the Midway3 pilot in ~/gc-endpoint and a
    # fresh ~/.gcx) must each keep their own entry. Run the real shell command
    # against a fake crontab.
    import subprocess
    tab = tmp_path / "tab"
    tab.write_text(
        "MAILTO=a@b.edu\n"
        "*/2 * * * * /home/alice/gc-endpoint/keepalive.sh primary # gcx-keepalive midway3\n"
        "*/2 * * * * /home/alice/.gcx/keepalive.sh primary # gcx-keepalive midway3\n"
        "*/2 * * * * /home/alice/x/keepalive.sh single # gcx-keepalive midway3 gcx2\n"
        "*/2 * * * * /home/alice/.gcx/keepalive.sh single # gcx-keepalive dsi gcx\n"
        "0 3 * * * ~/backup.sh\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "crontab").write_text(f'#!/bin/sh\n[ "$1" = -l ] && exec cat {tab}\n'
                                  f'cat > {tab}.new && mv {tab}.new {tab}\n')
    (fake / "crontab").chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin"}
    c = ctx({"keepalive": {"mode": "failover", "primary": "n4", "backup": "n3"}})
    for role in ("primary", "backup"):  # re-running replaces, never duplicates
        subprocess.run(["sh", "-c", steps._cron_cmd(c, role)], env=env, check=True)
    assert tab.read_text().splitlines() == [
        "MAILTO=a@b.edu",
        "*/2 * * * * /home/alice/gc-endpoint/keepalive.sh primary # gcx-keepalive midway3",
        "*/2 * * * * /home/alice/x/keepalive.sh single # gcx-keepalive midway3 gcx2",
        "*/2 * * * * /home/alice/.gcx/keepalive.sh single # gcx-keepalive dsi gcx",
        "0 3 * * * ~/backup.sh",
        "*/2 * * * * /home/alice/.gcx/keepalive.sh backup # gcx-keepalive midway3 gcx",
    ]


def test_endpoint_files_fill_workers_and_path():
    prof = dict(profiles.load("midway3"), max_workers=2)
    files = steps.endpoint_files(ctx(prof=prof))
    tmpl = files["~/.globus_compute/gcx/user_config_template.yaml.j2"]
    assert "max_workers: 2 " in tmpl and "@MAX_WORKERS@" not in tmpl
    assert "JSONData" in tmpl and "PureSourceTextInspect" in tmpl
    env = files["~/.globus_compute/gcx/user_environment.yaml"]
    assert "PATH: /home/alice/.gcx/venv/bin:/home/alice/.local/bin:/opt/slurm/bin:" in env


def test_personal_config_and_transfer_state_path():
    c = ctx(facts={"ep_id": "ep-1"})
    p = steps.personal(c)
    assert p["endpoint"] == "ep-1" and p["remote_state"] == "~/.gcx/state"
    assert p["state"] == "2fde89c0-6fb4-11eb-8c47-0eb1aa8d4337:/~/.gcx/state/"


def test_probe_blocks_unreachable_globus(monkeypatch):
    c = ctx()
    monkeypatch.setattr(c, "probe", lambda: None)
    c.facts.update(net_amqps_5671="blocked", net_amqps_443="blocked")
    assert steps.s_probe(c).status == steps.BLOCKED
    c.facts.update(net_amqps_443="ok")
    assert steps.s_probe(c).status == steps.OK
    c.facts.update(scheduler="pbs")
    assert steps.s_probe(c).status == steps.BLOCKED


def test_cron_step_targets_the_right_node():
    cfg = {"keepalive": {"mode": "failover", "primary": "midway3-login4",
                         "backup": "midway3-login3"}}
    c = ctx(cfg, facts={"heartbeats": "midway3-login4:30"})
    r = steps.s_cron(c)
    assert r.status == steps.HUMAN and "backup" in r.detail and "midway3-login3" in r.detail
    c.facts["heartbeats"] = "midway3-login4:30,midway3-login3:31"
    assert steps.s_cron(c).status == steps.OK
    c.facts["heartbeats"] = "midway3-login3:31"  # primary (this node) missing
    assert steps.s_cron(c).status == steps.CHANGE


def test_unsafe_root_refused():
    with pytest.raises(RemoteError):
        Remote("x").probe("~/a; rm -rf ~", "gcx")
    with pytest.raises(RemoteError):
        Remote("x").probe("/etc", "gcx")


def test_login_shell_variables_resolved_into_paths(capsys):
    facts = {"env_WORK": "/work2/09979/alice/stampede3", "env_SCRATCH": ""}
    roots = menu.resolve_roots(["~", "$WORK", "$WORK/runs/", "$SCRATCH", "/scratch/$USER"], facts)
    assert roots == ["~", "/work2/09979/alice/stampede3", "/work2/09979/alice/stampede3/runs",
                     "/scratch/$USER"]
    assert "dropping $SCRATCH" in capsys.readouterr().out


def typed(monkeypatch, *answers):
    """Feed answers to input(), in order; fail if the menu asks for more."""
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_typed_directory_is_added_to_defaults_not_replacing_them(monkeypatch):
    # The DSI case: typing one extra path used to drop ~ and scratch.
    typed(monkeypatch, "/net/monsoon", "")  # add, then Enter at "Use this?"
    got = menu.Prompter().extend_list("Directories gcx may read under", ["~", "/net/scratch/$USER"])
    assert got == ["~", "/net/scratch/$USER", "/net/monsoon"]


def test_minus_removes_a_default_and_duplicates_are_ignored(monkeypatch):
    typed(monkeypatch, "-~, /net/monsoon, /net/scratch/$USER", "y")
    got = menu.Prompter().extend_list("Dirs", ["~", "/net/scratch/$USER"])
    assert got == ["/net/scratch/$USER", "/net/monsoon"]


def test_enter_keeps_defaults(monkeypatch):
    typed(monkeypatch, "", "")
    assert menu.Prompter().extend_list("Dirs", ["~"]) == ["~"]


def test_rejecting_the_final_list_asks_again(monkeypatch):
    typed(monkeypatch, "/wrong", "n", "/right", "y")
    assert menu.Prompter().extend_list("Dirs", ["~"]) == ["~", "/right"]


def test_accounts_still_replace_but_are_confirmed(monkeypatch):
    typed(monkeypatch, "pi-b", "n", "pi-a, pi-b", "")
    assert menu.Prompter().choose_list("Accounts", ["pi-a", "pi-b", "pi-c"]) == ["pi-a", "pi-b"]


def test_full_menu_with_typed_answers(monkeypatch):
    # submit? run? read? shell? | read dirs: add, confirm | script dirs: Enter, confirm
    # | accounts: Enter, confirm | queues: Enter, confirm | qos: Enter, confirm
    typed(monkeypatch, "y", "n", "y", "n", "/net/monsoon", "", "", "", "", "", "", "", "", "")
    c = ctx()
    pol = menu.build_policy("midway3", c.prof, c.facts, c.root, menu.Prompter())
    assert pol["read_roots"] == ["~", "/scratch/midway3/$USER", "/net/monsoon"]
    assert pol["script_roots"] == ["~", "/scratch/midway3/$USER"]
    assert pol["capabilities"] == ["status", "submit", "read"]


def test_cron_denied_and_on_use_declined_gives_none(monkeypatch):
    typed(monkeypatch, "n")
    c = ctx(facts={"cron": "denied"})
    assert menu.choose_keepalive("delta", c.prof, c.facts, menu.Prompter()) == {"mode": "none"}


def test_on_use_cluster_skips_cron_steps():
    c = ctx({"keepalive": {"mode": "on-use"}})
    assert steps.s_keepalive_files(c).status == steps.OK
    r = steps.s_cron(c)
    assert r.status == steps.OK and "restarts the endpoint over SSH" in r.detail


def test_yes_typed_at_add_directories_is_rejected_and_reasked(monkeypatch, capsys):
    # Stampede3, 2026-10-02: "y" was saved as a script directory.
    typed(monkeypatch, "y", "", "")
    assert menu.Prompter().extend_list("Dirs", ["~"]) == ["~"]
    assert 'looks like an answer to "Use this?"' in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["relative/dir", "/", "x y"])
def test_non_paths_rejected(monkeypatch, bad):
    typed(monkeypatch, bad, "/ok", "")
    assert menu.Prompter().extend_list("Dirs", ["~"]) == ["~", "/ok"]


def test_qos_none_means_empty(monkeypatch):
    typed(monkeypatch, "none", "")
    assert menu.Prompter().choose_list("QoS values", []) == []


def test_invalid_saved_policy_sends_setup_back_to_the_menu():
    c = ctx({"policy": {"scheduler": "slurm", "capabilities": ["status", "submit"],
                        "script_roots": ["~", "y"]}})
    r = steps.s_policy(c)
    assert r.status == steps.HUMAN and "invalid" in r.detail and "'y'" in r.detail


def test_account_spelling_follows_policy(monkeypatch):
    from gcx import cli, config
    monkeypatch.setattr(config, "load", lambda c: {"policy": {"scheduler": "slurm",
                                                              "accounts": ["tg-atm170020"]}})
    assert cli._account_spelling("stampede3", "TG-ATM170020") == "tg-atm170020"
    assert cli._account_spelling("stampede3", "other") == "other"


def test_tacc_accounts_offered_in_upper_case_without_default(monkeypatch):
    # Stampede3, 2026-10-02: "default" is not a real account, and TACC's submit
    # filter rejects the lower-case project name Slurm reports.
    prof = dict(profiles.load("stampede3"))
    assert prof["accounts_upper"]
    c = ctx(prof=prof, facts={"accounts": "default,tg-atm170020"})
    pol = menu.build_policy("stampede3", prof, c.facts, c.root, c.ask)
    assert pol["accounts"] == ["TG-ATM170020"]


def test_worker_env_copied_from_login_shell():
    prof = dict(profiles.load("stampede3"))
    c = ctx(prof=prof, facts={"env_WORK2": "/work2/1/alice/stampede3", "env_STOCKYARD": "/work2/1/alice"})
    env = steps.endpoint_files(c)["~/.globus_compute/gcx/user_environment.yaml"]
    assert "WORK2: /work2/1/alice/stampede3\n" in env and "STOCKYARD: /work2/1/alice\n" in env
    assert "ARCHIVE" not in env  # unset on the cluster: left out


def test_unsafe_env_var_names_refused():
    with pytest.raises(RemoteError):
        Remote("x").probe("~/.gcx", "gcx", env_vars=["WORK; rm -rf ~"])


def test_pbs_job_completion_and_ncar_projects():
    assert steps.done("Job Id: 1.desched1\n    job_state = F\n    Exit_status = 0\n", "pbs")
    assert not steps.done("Job Id: 1.desched1\n    job_state = R\n", "pbs")
    assert steps.done("123  test  cpu  COMPLETED", "slurm")
    prof = dict(profiles.load("derecho"))
    c = ctx(prof=prof, facts={"scheduler": "pbs", "accounts": "", "groups": "ncar,uchi0014,uchi0018,docker"})
    pol = menu.build_policy("derecho", prof, c.facts, c.root, c.ask)
    assert pol["accounts"] == ["UCHI0014", "UCHI0018"]


def test_trailing_slash_is_not_a_new_directory(monkeypatch):
    # Polaris, 2026-10-02: typing "~/" next to the default "~" saved "~" twice.
    typed(monkeypatch, "~/, /eagle/proj/", "")
    got = menu.Prompter().extend_list("Dirs", ["~"])
    assert got == ["~", "/eagle/proj/"]
    assert menu.resolve_roots(got + ["~/"], {}) == ["~", "/eagle/proj"]


def test_menu_again_prefills_current_answers(monkeypatch):
    cur = {"scheduler": "slurm", "capabilities": ["status", "read"],
           "read_roots": ["~", "/net/monsoon"]}
    # submit? (default now no) run? (no) read? (yes) shell? (no) | read dirs: add one, confirm
    typed(monkeypatch, "", "", "", "", "/net/scratch/$USER", "")
    c = ctx()
    pol = menu.build_policy("dsi", c.prof, c.facts, c.root, menu.Prompter(), current=cur)
    assert pol["capabilities"] == ["status", "read"]
    assert pol["read_roots"] == ["~", "/net/monsoon", "/net/scratch/$USER"]


def test_run_menu_adds_a_launcher_and_asks_accounts(monkeypatch):
    # submit? run? read? shell? | add launcher? root, command, timeout (bad, then good),
    # block risky?, add another? | accounts: Enter, confirm | queues: type, confirm
    # | qos: Enter, confirm
    typed(monkeypatch, "n", "y", "n", "n",
          "y", "~/repo", "python3 tools/render.py --dry-run", "soon", "30", "", "n",
          "", "", "caslake", "", "", "")
    c = ctx()
    pol = menu.build_policy("midway3", c.prof, c.facts, c.root, menu.Prompter())
    assert pol["capabilities"] == ["status", "run"]
    assert pol["launchers"] == [{"root": "~/repo", "block_risky": True, "timeout": 30,
                                 "argv": ["python3", "tools/render.py", "--dry-run"]}]
    assert pol["accounts"] == ["pi-a", "pi-b"] and pol["queues"] == ["caslake"]
    assert "script_roots" not in pol
    build.validate(pol)


def test_run_launcher_defaults_to_cat(monkeypatch):
    # Enter at the command and timeout: submit scripts as written, 120 s.
    typed(monkeypatch, "y", "~/repo", "", "", "", "n")
    launchers = menu.choose_launchers(menu.Prompter(), [], {})
    assert launchers == [{"root": "~/repo", "argv": ["cat"], "timeout": 120, "block_risky": True}]


def test_run_without_launchers_is_turned_off(capsys):
    c = ctx()  # --yes: no launchers can be typed
    pol = menu.build_policy("midway3", c.prof, c.facts, c.root, menu.Prompter(True),
                            current={"scheduler": "slurm", "capabilities": ["status", "run"]})
    assert "run" not in pol["capabilities"] and "launchers" not in pol
    assert "run stays off" in capsys.readouterr().out


def test_run_not_offered_on_pbs(capsys):
    prof = profiles.load("derecho")
    c = ctx(prof=prof, facts={"scheduler": "pbs", "accounts": "", "groups": "uchi0014"})
    menu.build_policy("derecho", prof, c.facts, c.root, c.ask)
    assert "Run scripts with arguments" not in capsys.readouterr().out


def test_running_elsewhere_per_globus_is_not_started_again(monkeypatch):
    # Derecho, 2026-10-06: SSH landed on derecho5, the endpoint ran on derecho2,
    # and setup started a second copy.
    from gcx import restart
    c = ctx({"endpoint": "ep-1", "keepalive": {"mode": "on-use"}},
            facts={"ep_running": "no", "ep_id": "ep-1", "host": "derecho5"})
    monkeypatch.setattr(c, "get_client", lambda: None)
    monkeypatch.setattr(restart, "status", lambda client, ep: "online")
    r = steps.s_running(c)
    assert r.status == steps.OK and "another login node" in r.detail
    monkeypatch.setattr(restart, "status", lambda client, ep: "offline")
    r = steps.s_running(c)
    assert r.status == steps.CHANGE and r.action is not None


def test_doctor_reports_endpoint_errors_instead_of_crashing(monkeypatch, capsys):
    from gcx import doctor

    def boom(client, cluster):
        yield ("functions registered for current policy", True, "ok")
        raise RuntimeError("Another instance of this endpoint is running. (Perhaps on another login node?)")
    monkeypatch.setattr(doctor, "checks", boom)
    assert doctor.main(None, "derecho") == 1
    out = capsys.readouterr().out
    assert "FAIL  endpoint ran the check" in out and "gcx setup derecho" in out


def test_stale_lease_without_cron_does_not_count_as_running(monkeypatch):
    # Derecho: setup's start wrote the lease once; the endpoint then died.
    from gcx import restart
    c = ctx({"endpoint": "ep-1", "keepalive": {"mode": "on-use"}},
            facts={"ep_running": "no", "ep_id": "ep-1", "owner": "derecho5", "owner_age": "196"})
    monkeypatch.setattr(c, "get_client", lambda: None)
    monkeypatch.setattr(restart, "status", lambda client, ep: "offline")
    assert steps.s_running(c).status == steps.CHANGE
    c.cfg["keepalive"] = {"mode": "single", "primary": "derecho5"}
    assert steps.s_running(c).status == steps.OK  # with cron, a fresh lease means held


def test_probe_lists_gcx_endpoints_under_any_name(tmp_path):
    import subprocess
    from importlib import resources
    from gcx import allowlist
    gc = tmp_path / ".globus_compute"
    for name, ep_id, fns in [("gcx", "ep-new", ["f1", "f2"]), ("midway3-login", "ep-old", ["f3"]),
                             ("uep.ep-old.x", "ep-old", ["f3"])]:
        (gc / name).mkdir(parents=True)
        (gc / name / "endpoint.json").write_text(f'{{"endpoint_id": "{ep_id}"}}')
        (gc / name / "config.yaml").write_text(
            allowlist.render("display_name: x\nengine:\n  type: GlobusComputeEngine\n", set(fns)))
    (gc / "mine").mkdir()  # someone's own endpoint, not gcx's
    (gc / "mine" / "config.yaml").write_text("display_name: mine\n")
    stub = tmp_path / "bin"
    stub.mkdir()
    for tool in ("timeout", "crontab", "sbatch", "qsub"):  # no network, no real crontab
        (stub / tool).write_text("#!/bin/sh\nexit 1\n")
        (stub / tool).chmod(0o755)
    script = resources.files("gcx.setup").joinpath("probe.sh").read_text()
    out = subprocess.run(["bash", "-s"], input='GCX_ROOT="$HOME/.gcx"\nGCX_EP=gcx\n' + script,
                         capture_output=True, text=True, check=True,
                         env={"HOME": str(tmp_path), "PATH": f"{stub}:/usr/bin:/bin",
                              "USER": "alice"}).stdout
    facts = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)
    assert steps.gcx_endpoints(facts) == {"gcx": ("ep-new", {"f1", "f2"}),
                                          "midway3-login": ("ep-old", {"f3"})}


def test_setup_refuses_another_computers_endpoint():
    # Midway3, 2026-11-04: a second laptop, with no gcx config, set up beside the first.
    first = "midway3-login|ep-old|f1,f2"
    c = ctx(facts={"gcx_endpoints": first})
    r = steps.s_other_computers(c)
    assert r.status == steps.BLOCKED and "second endpoint beside it" in r.detail
    assert "section 6" in r.detail and "--take-over" in r.detail
    c = ctx(facts={"gcx_endpoints": "gcx|ep-old|f1,f2"})  # same default name
    assert "so that computer's gcx calls would be refused" in steps.s_other_computers(c).detail
    c.take_over = True
    assert steps.s_other_computers(c).status == steps.OK


def test_setup_proceeds_on_its_own_endpoint():
    assert steps.s_other_computers(ctx()).status == steps.OK  # nothing there yet
    assert steps.s_other_computers(ctx(facts={"gcx_endpoints": ""})).status == steps.OK
    # The computer that set it up, or one given a copy of its config.
    c = ctx({"endpoint": "ep-old"}, facts={"gcx_endpoints": "midway3-login|ep-old|f1"})
    assert steps.s_other_computers(c).status == steps.OK
    # A first setup interrupted after the allowlist was written, before the ID was recorded.
    c = ctx({"functions": {"gcx_ping": {"uuid": "f1", "sha256": "h"}}},
            facts={"gcx_endpoints": "gcx||f1,f2"})
    assert steps.s_other_computers(c).status == steps.OK
