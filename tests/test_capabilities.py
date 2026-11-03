"""The registered capability code, exercised the way the endpoint runs it.

Each test builds the source for a policy with build.source() and exec's it
(PureSourceTextInspect does exactly this), with scheduler commands replaced by
tests/fakebin stubs that log their argv. Real files in tmp_path stand in for
home, scratch and an outside directory.
"""

import os
from pathlib import Path

import pytest

from gcx.capabilities import build

FAKEBIN = Path(__file__).parent / "fakebin"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, scratch, outside = (tmp_path / d for d in ("home", "scratch", "outside"))
    for d in (home, scratch, outside):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("SCRATCH", str(scratch))
    monkeypatch.setenv("PATH", f"{FAKEBIN}:{os.environ['PATH']}")
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    return type("Env", (), dict(home=home, scratch=scratch, outside=outside, log=log))


def load(**overrides):
    policy = {
        "scheduler": "slurm",
        "capabilities": ["status", "submit", "read"],
        "read_roots": ["~", "$SCRATCH"],
        "script_roots": ["~"],
        "accounts": ["pi-good"],
        "queues": ["caslake"],
        "qos": ["schmidt"],
    }
    policy.update(overrides)
    ns = {}
    exec(build.source(policy), ns)
    return ns


def calls(env):
    """Logged invocations as [(cwd, [argv...]), ...], argv[0] as a bare name."""
    out, cur = [], None
    for line in env.log.read_text().splitlines():
        if line.startswith("cwd="):
            cur = (line[4:], [])
        elif line == "--":
            out.append(cur)
        elif not cur[1]:
            cur[1].append(os.path.basename(line))
        else:
            cur[1].append(line)
    return out


def refused(res):
    return res.get("refused") and res["rc"] == 2


# ---- build -----------------------------------------------------------------

def test_source_embeds_policy_and_parses_on_oldest_python():
    text = build.source({"scheduler": "pbs", "capabilities": ["status"]})
    assert "POLICY = {'capabilities': ['status'], 'scheduler': 'pbs'}" in text
    assert build.sha256(text) == build.sha256(build.source(
        {"capabilities": ["status"], "scheduler": "pbs"}))  # key order irrelevant


@pytest.mark.parametrize("policy, msg", [
    ({"scheduler": "lsf", "capabilities": ["status"]}, "scheduler"),
    ({"scheduler": "slurm", "capabilities": ["read"]}, "always on"),
    ({"scheduler": "slurm", "capabilities": ["status", "read"]}, "read_roots"),
    ({"scheduler": "slurm", "capabilities": ["status", "read"], "read_roots": ["/"]}, "not /"),
    ({"scheduler": "slurm", "capabilities": ["status", "read"], "read_roots": ["rel"]}, "absolute"),
    ({"scheduler": "slurm", "capabilities": ["status", "nuke"]}, "unknown"),
    ({"scheduler": "slurm", "capabilities": ["status"], "x": Path("/")}, "only strings"),
])
def test_bad_policies_rejected(policy, msg):
    with pytest.raises(build.PolicyError, match=msg):
        build.source(policy)


def test_entry_functions_follow_capabilities():
    fns = build.entry_functions({"capabilities": ["status", "read"]})
    assert "gcx_read" in fns and "gcx_submit" not in fns and "gcx_shell" not in fns


# ---- read --------------------------------------------------------------------

def test_read_tail_and_head(env):
    (env.home / "log.out").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    ns = load()
    tail = ns["gcx_read"]("log.out", "tail", 3)  # relative = under home
    assert tail["stdout"] == "line 998\nline 999\nline 1000\n"
    head = ns["gcx_read"]("~/log.out", "head", 2)
    assert head["stdout"] == "line 1\nline 2\n"


def test_tail_of_short_file_and_file_without_final_newline(env):
    (env.home / "a").write_text("x\ny")
    ns = load()
    assert ns["gcx_read"]("~/a", "tail", 10)["stdout"] == "x\ny"
    assert ns["gcx_read"]("~/a", "tail", 1)["stdout"] == "y"


def test_read_outside_roots_refused(env):
    (env.outside / "secret").write_text("s")
    ns = load()
    assert refused(ns["gcx_read"]("/etc/passwd"))
    assert refused(ns["gcx_read"](str(env.outside / "secret")))
    assert refused(ns["gcx_read"]("~/../outside/secret"))


def test_symlink_escaping_roots_refused(env):
    (env.outside / "secret").write_text("s")
    (env.home / "link").symlink_to(env.outside / "secret")
    (env.home / "dirlink").symlink_to(env.outside)
    ns = load()
    assert refused(ns["gcx_read"]("~/link"))
    assert refused(ns["gcx_ls"]("~/dirlink"))


def test_env_var_root(env):
    (env.scratch / "run").mkdir()
    ns = load()
    res = ns["gcx_ls"]("$SCRATCH")
    assert res["rc"] == 0 and [e["name"] for e in res["entries"]] == ["run"]


def test_read_disabled_when_not_in_policy(env):
    (env.home / "f").write_text("x")
    ns = load(capabilities=["status"])
    assert refused(ns["gcx_read"]("~/f"))


def test_read_bad_args(env):
    (env.home / "f").write_text("x")
    ns = load()
    assert refused(ns["gcx_read"]("~/f", "tail", 0))
    assert refused(ns["gcx_read"]("~/f", "cat", 5))
    assert refused(ns["gcx_read"]("~"))  # a directory
    assert refused(ns["gcx_du"]("~", 9))


# ---- submit / cancel -------------------------------------------------------

def test_submit_slurm_argv(env):
    (env.home / "jobs").mkdir()
    script = env.home / "jobs" / "run.sbatch"
    script.write_text("#!/bin/bash\n")
    ns = load()
    res = ns["gcx_submit"]("~/jobs/run.sbatch", account="pi-good", queue="caslake",
                           walltime="00:10:00", depends_on=["11", "12"], job_name="t1",
                           qos="schmidt")
    assert res["rc"] == 0 and res["job_id"] == "4242"
    [(cwd, argv)] = calls(env)
    assert cwd == str(script.parent.resolve())
    assert argv == ["sbatch", "--parsable", "-A", "pi-good", "-p", "caslake", "--qos", "schmidt",
                    "-t", "00:10:00", "--dependency=afterok:11:12", "-J", "t1",
                    str(script.resolve())]


def test_submit_pbs_argv_with_site_extra(env):
    script = env.home / "run.pbs"
    script.write_text("#!/bin/bash\n")
    ns = load(scheduler="pbs", queues=["debug"],
              submit_extra=["-l", "filesystems=home:eagle"])
    res = ns["gcx_submit"]("~/run.pbs", queue="debug", walltime="01:00:00")
    assert res["job_id"] == "4242.polaris-pbs-01"
    [(_, argv)] = calls(env)
    assert argv == ["qsub", "-q", "debug", "-l", "walltime=01:00:00",
                    "-l", "filesystems=home:eagle", str(script.resolve())]


@pytest.mark.parametrize("kwargs", [
    {"account": "pi-other"},
    {"queue": "bigmem"},
    {"qos": "high"},
    {"walltime": "10:00; rm -rf ~"},
    {"job_name": "x y"},
    {"job_name": "--wrap=id"},
    {"depends_on": ["1; id"]},
])
def test_submit_bad_args_refused_without_calling_sbatch(env, kwargs):
    (env.home / "run.sbatch").write_text("#!/bin/bash\n")
    ns = load()
    assert refused(ns["gcx_submit"]("~/run.sbatch", **kwargs))
    assert not env.log.exists()


def test_submit_script_must_be_under_script_roots(env):
    (env.scratch / "run.sbatch").write_text("#!/bin/bash\n")  # readable, not submittable
    ns = load()
    assert refused(ns["gcx_submit"]("$SCRATCH/run.sbatch"))
    assert refused(ns["gcx_submit"]("~/missing.sbatch"))


def test_cancel_only_own_jobs(env):
    ns = load()
    assert ns["gcx_cancel"](["123", "124_7"])["rc"] == 0
    [(_, argv)] = calls(env)
    assert argv == ["scancel", "-u", "alice", "123", "124_7"]
    assert refused(ns["gcx_cancel"](["123 -u bob"]))
    assert refused(ns["gcx_cancel"]([]))


def test_submit_disabled(env):
    (env.home / "run.sbatch").write_text("#!/bin/bash\n")
    ns = load(capabilities=["status", "read"])
    assert refused(ns["gcx_submit"]("~/run.sbatch"))
    assert refused(ns["gcx_cancel"](["1"]))


# ---- status ---------------------------------------------------------------

def test_jobs_and_history_argv(env):
    ns = load()
    ns["gcx_jobs"]()
    ns["gcx_jobs"](["5"])
    ns["gcx_history"](since="2026-10-01")
    (_, a), (_, b), (_, c) = calls(env)
    assert a[:1] == ["squeue"] and a[-2:] == ["-u", "alice"]
    assert b[-2:] == ["-j", "5"]
    assert c[0] == "sacct" and c[-2:] == ["-S", "2026-10-01"]
    assert refused(ns["gcx_history"](since="yesterday; id"))


def test_pbs_status_argv(env):
    ns = load(scheduler="pbs")
    ns["gcx_jobs"]()
    ns["gcx_history"](["99.pbs"])
    (_, a), (_, b) = calls(env)
    assert a == ["qstat", "-u", "alice"]
    assert b == ["qstat", "-x", "-f", "99.pbs"]


def test_ping(env):
    ping = load()["gcx_ping"]()
    assert ping["user"] == "alice" and len(ping["policy_sha256"]) == 64
    assert ping["capabilities"] == ["status", "submit", "read"]


# ---- shell -----------------------------------------------------------------

def test_shell_only_when_enabled(env):
    assert refused(load()["gcx_shell"]("echo hi"))
    res = load(capabilities=["status", "shell"])["gcx_shell"]("echo hi; pwd")
    assert res["rc"] == 0 and res["stdout"] == f"hi\n{env.home}\n"


def test_job_id_after_site_banner(env, tmp_path):
    # TACC prints a banner and an env dump to stdout before sbatch's own line.
    (env.home / "run.sbatch").write_text("#!/bin/bash\n")
    fake = tmp_path / "bin2"
    fake.mkdir()
    (fake / "sbatch").write_text("#!/bin/sh\necho '---- Welcome ----'\necho 'SLURM_TACC_NODES=1'\necho 3557959\n")
    (fake / "sbatch").chmod(0o755)
    os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
    res = load()["gcx_submit"]("~/run.sbatch")
    assert res["job_id"] == "3557959"


# ---- run (templated, Slurm) -------------------------------------------------

HEADER = ("#!/bin/bash\n"
          "#SBATCH --account=pi-good\n"
          "#SBATCH -p caslake   # partition\n"
          "#SBATCH --job-name=smoke --output=logs/%x-%j.out\n"
          "\n")


def load_run(env, script=HEADER + "pytest \"$@\"\n", **launcher):
    repo = env.home / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "smoke.sh").write_text(script)
    ns = load(capabilities=["status", "run"],
              launchers=[dict({"root": "~/repo", "argv": ["fake-launch", "--dry-run"]}, **launcher)])
    return ns, repo


def test_run_renders_checks_and_submits_with_args(env):
    ns, repo = load_run(env)
    res = ns["gcx_run"]("~/repo/scripts/smoke.sh", args=["-k", "foo bar"], walltime="00:30:00",
                        depends_on=["7"])
    assert res["rc"] == 0 and res["job_id"] == "4242"
    assert res["output"] == f"{repo.resolve()}/logs/smoke-4242.out"
    assert (repo / "logs").is_dir()  # sbatch would not create it
    (lcwd, launch), (scwd, sbatch) = calls(env)
    assert lcwd == scwd == str(repo.resolve())
    assert launch == ["fake-launch", "--dry-run", str(repo.resolve() / "scripts" / "smoke.sh")]
    tmp = sbatch[-3]
    assert sbatch == ["sbatch", "--parsable", "-A", "pi-good", "-p", "caslake", "-t", "00:30:00",
                      "--dependency=afterok:7", "-J", "smoke", tmp, "-k", "foo bar"]
    assert os.path.basename(tmp).startswith("gcx-run-") and not os.path.exists(tmp)


def test_run_with_cat_submits_the_script_as_written(env):
    # The setup default: no renderer, nothing needed beyond coreutils.
    ns, repo = load_run(env, argv=["cat"])
    res = ns["gcx_run"]("~/repo/scripts/smoke.sh", args=["a b", "c"])
    assert res["rc"] == 0 and res["job_id"] == "4242"
    [(cwd, sbatch)] = calls(env)  # cat is not a fake, so only sbatch is logged
    assert cwd == str(repo.resolve())
    assert sbatch[:6] == ["sbatch", "--parsable", "-A", "pi-good", "-p", "caslake"]
    assert sbatch[-2:] == ["a b", "c"]


def test_run_overrides_beat_the_header_and_are_checked(env):
    ns, _ = load_run(env)
    assert refused(ns["gcx_run"]("~/repo/scripts/smoke.sh", queue="bigmem"))
    ns = load(capabilities=["status", "run"], queues=["caslake", "debug"],
              launchers=[{"root": "~/repo", "argv": ["fake-launch"]}])
    res = ns["gcx_run"]("~/repo/scripts/smoke.sh", queue="debug", job_name="mine")
    assert res["rc"] == 0
    sbatch = calls(env)[-1][1]
    assert sbatch[4:6] == ["-p", "debug"] and sbatch[6:8] == ["-J", "mine"]
    assert res["output"].endswith("/logs/mine-4242.out")


@pytest.mark.parametrize("header, msg", [
    ("#!/bin/bash\n#SBATCH -p caslake\n", "no account"),
    ("#!/bin/bash\n#SBATCH -A pi-good\n", "no partition"),
    ("#!/bin/bash\n#SBATCH -A pi-other -p caslake\n", "account 'pi-other' not allowed"),
    ("#!/bin/bash\n#SBATCH --acc=pi-other -p caslake\n", "not allowed"),  # sbatch abbreviation
    ("#!/bin/bash\n#SBATCH -Api-good -pbigmem\n", "queue 'bigmem' not allowed"),
    ("#!/bin/bash\n#SBATCH -A pi-good -p caslake --qos=high\n", "qos 'high' not allowed"),
    ("#!/bin/bash\n#SBATCH -A pi-good -p caslake\n#SBATCH --export=LD_PRELOAD=/x.so\n", "--export"),
    ("#!/bin/bash\n#SBATCH -A pi-good -p caslake\n#SBATCH --exp=ALL\n", "--exp"),
    ("#!/bin/bash\n#SBATCH -A pi-good -p caslake --wrap 'id'\n", "--wrap"),
    ("#!/bin/bash\n#SBATCH -A pi-good -p caslake\n#SBATCH --uid=0\n", "--uid"),
    ("#!/bin/bash\n#SBATCH -A pi-good -p caslake\n#SBATCH --get-user-env\n", "--get-user-env"),
    ("echo not a script\n", "not a complete batch script"),
])
def test_run_refuses_bad_rendered_scripts_without_sbatch(env, header, msg):
    ns, _ = load_run(env, script=header + "true\n")
    res = ns["gcx_run"]("~/repo/scripts/smoke.sh")
    assert refused(res) and msg in res["stderr"]
    assert [argv[0] for _, argv in calls(env)] == ["fake-launch"]


def test_run_header_stops_at_first_command(env):
    # sbatch ignores #SBATCH lines after the first command, and so does gcx.
    ns, _ = load_run(env, script=HEADER + "true\n#SBATCH --export=ALL\n")
    assert ns["gcx_run"]("~/repo/scripts/smoke.sh")["rc"] == 0


def test_run_blocklist_can_be_turned_off_per_launcher(env):
    ns, _ = load_run(env, script=HEADER.replace("\n\n", "\n#SBATCH --export=NONE\n\n") + "true\n",
                     block_risky=False)
    assert ns["gcx_run"]("~/repo/scripts/smoke.sh")["rc"] == 0


@pytest.mark.parametrize("kwargs", [
    {"args": "-k foo"},
    {"args": ["ok", 3]},
    {"args": ["x"] * 65},
    {"args": ["x" * 4097]},
    {"args": ["a\x00b"]},
    {"walltime": "1:00; id"},
    {"job_name": "--wrap=id"},
    {"depends_on": ["1; id"]},
])
def test_run_bad_request_refused_before_the_launcher(env, kwargs):
    ns, _ = load_run(env)
    assert refused(ns["gcx_run"]("~/repo/scripts/smoke.sh", **kwargs))
    assert not env.log.exists()


def test_run_script_must_be_under_a_launcher_root(env):
    ns, _ = load_run(env)
    (env.home / "other.sh").write_text(HEADER)
    res = ns["gcx_run"]("~/other.sh")
    assert refused(res) and "not under a launcher root" in res["stderr"]
    (env.home / "repo" / "escape.sh").symlink_to(env.home / "other.sh")
    assert refused(ns["gcx_run"]("~/repo/escape.sh"))
    assert not env.log.exists()


def test_run_launcher_failure_is_reported(env, monkeypatch):
    ns, _ = load_run(env)
    monkeypatch.setenv("FAKE_LAUNCH_FAIL", "1")
    res = ns["gcx_run"]("~/repo/scripts/smoke.sh")
    assert res["rc"] == 3 and "launcher failed" in res["stderr"] and "template error" in res["stderr"]
    assert [argv[0] for _, argv in calls(env)] == ["fake-launch"]


def test_log_dir_outside_root_not_created(env):
    ns, _ = load_run(env, script=HEADER.replace("logs/%x-%j.out", f"{env.scratch}/new/%j.out")
                     + "true\n")
    res = ns["gcx_run"]("~/repo/scripts/smoke.sh")
    assert res["output"] == f"{env.scratch}/new/4242.out" and not (env.scratch / "new").exists()


def test_run_disabled_and_policy_rules(env):
    assert refused(load()["gcx_run"]("~/repo/scripts/smoke.sh"))
    base = {"scheduler": "slurm", "capabilities": ["status", "run"], "accounts": ["a"],
            "queues": ["q"], "launchers": [{"root": "~/repo", "argv": ["x"]}]}
    build.validate(base)
    for bad, msg in [({"scheduler": "pbs"}, "Slurm-only"),
                     ({"accounts": []}, "accounts and queues"),
                     ({"launchers": []}, "needs launchers"),
                     ({"launchers": [{"root": "/", "argv": ["x"]}]}, "not /"),
                     ({"launchers": [{"root": "~/r", "argv": []}]}, "argv"),
                     ({"launchers": [{"root": "~/r", "argv": ["x"], "shell": True}]}, "keys"),
                     ({"launchers": [{"root": "~/r", "argv": ["x"], "block_risky": "no"}]}, "true or false"),
                     ({"launchers": [{"argv": ["x"]}]}, "must be an absolute")]:
        with pytest.raises(build.PolicyError, match=msg):
            build.validate(dict(base, **bad))
    assert "gcx_run" in build.entry_functions(base) and "gcx_run" in build.MUTATING
