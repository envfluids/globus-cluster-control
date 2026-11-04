"""`gcx login globus [--check]` against a fake Globus CLI that behaves like the real
one: exit 4 with MissingLoginError before login, and a data_access consent error
from `ls` until that collection is consented."""

import os

import pytest

from gcx import cli, config, globuscli
from gcx.setup import menu, steps

MIDWAY3 = "2fde89c0-6fb4-11eb-8c47-0eb1aa8d4337"
STAMPEDE3 = "1e9ddd41-fe4b-406f-95ff-f3d79f9cb523"

FAKE = r"""#!/bin/sh
echo "$*" >> "$STATE/calls"
case "$1" in
  whoami)
    [ -f "$STATE/logged-in" ] && { echo alice@uchicago.edu; exit 0; }
    echo "MissingLoginError: Missing login for auth.globus.org" >&2; exit 4 ;;
  login)
    touch "$STATE/logged-in"; exit 0 ;;
  session)
    for s in "$@"; do
      case "$s" in https://*) touch "$STATE/ok-$(echo "$s" | sed 's|.*/scopes/||; s|/.*||')" ;; esac
    done; exit 0 ;;
  ls)
    [ -f "$STATE/logged-in" ] || { echo "MissingLoginError" >&2; exit 4; }
    uuid=${2%%:*}
    [ -f "$STATE/ok-$uuid" ] && exit 0
    echo "The collection you are trying to access data on requires you to grant consent" >&2
    exit 4 ;;
esac
exit 2
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    for c in ("midway3", "stampede3", "deltaai"):  # deltaai: no collection
        config.save(c, {"endpoint": "e", "keepalive": {"mode": "failover"}})
    state = tmp_path / "state"
    state.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "globus").write_text(FAKE)
    (bin_ / "globus").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    monkeypatch.setenv("STATE", str(state))
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = state / "calls"
    return type("F", (), {"state": state,
                          "calls": lambda: calls.read_text().splitlines() if calls.exists() else []})


def test_collections_are_the_configured_clusters_profiles(fake):
    assert globuscli.collections() == {"midway3": (MIDWAY3, "/~/"), "stampede3": (STAMPEDE3, "/~/")}


def test_check_reports_without_prompting(fake, capsys):
    assert globuscli.check() == 1
    assert "not logged in: `gcx login globus`" in capsys.readouterr().out
    (fake.state / "logged-in").touch()
    (fake.state / f"ok-{MIDWAY3}").touch()
    assert globuscli.check() == 1
    out = capsys.readouterr().out
    assert "ok      midway3" in out and "missing stampede3" in out and "deltaai" not in out
    assert not any(c.startswith(("login", "session")) for c in fake.calls())  # never prompts


def test_fix_logs_in_then_consents_only_what_is_missing(fake, monkeypatch, capsys):
    (fake.state / f"ok-{MIDWAY3}").touch()  # consented earlier, before a logout
    assert globuscli.fix() == 0
    prompts = [c for c in fake.calls() if c.startswith(("login", "session"))]
    assert prompts == ["login --no-local-server",
                       f"session consent --no-local-server "
                       f"https://auth.globus.org/scopes/{STAMPEDE3}/data_access"]
    assert "ok      midway3" in capsys.readouterr().out
    assert globuscli.check() == 0
    before = len(fake.calls())
    assert globuscli.fix() == 0  # nothing left to ask for
    assert not any(c.startswith(("login", "session")) for c in fake.calls()[before:])


def test_without_the_cli_or_without_collections(fake, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(config, "globus_cli", lambda: str(tmp_path / "nowhere" / "globus"))
    assert globuscli.check() == 1 and globuscli.fix() == 1
    assert globuscli.INSTALL in capsys.readouterr().out
    for c in ("midway3", "stampede3"):
        config.cluster_file(c).unlink()
    assert globuscli.check() == 0
    assert "nothing in gcx needs the Globus CLI" in capsys.readouterr().out


def test_gcx_login_globus_dispatch(fake, monkeypatch):
    ran = []
    monkeypatch.setattr(globuscli, "check", lambda: ran.append("check") or 0)
    monkeypatch.setattr(globuscli, "fix", lambda: ran.append("fix") or 0)
    monkeypatch.setattr(os, "execvpe", lambda *a: ran.append("ssh-sweep"))
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **kw: ran.append(("ssh", argv[2:])) or
                        type("P", (), {"returncode": 0})())
    for args in (["globus", "--check"], ["globus"], ["midway3", "globus"]):
        with pytest.raises(SystemExit):
            cli.login(args)
    assert ran == ["check", "fix", ("ssh", ["midway3"]), "fix"]


def test_status_without_the_cli_says_how_to_get_it(fake, monkeypatch, capsys, tmp_path):
    config.save("midway3", {"endpoint": "e", "state": f"{MIDWAY3}:/~/gc/state/"})
    monkeypatch.setattr(config, "globus_cli", lambda: str(tmp_path / "nowhere" / "globus"))
    assert cli.status("midway3") == 1
    assert "gcx login globus" in capsys.readouterr().err


def test_status_without_consent_points_to_gcx_login_globus(fake, capsys):
    (fake.state / "logged-in").touch()
    config.save("midway3", {"endpoint": "e", "state": f"{MIDWAY3}:/~/gc/state/"})
    assert cli.status("midway3") == 1
    assert "run `gcx login globus`" in capsys.readouterr().err


def test_setup_offers_it_only_where_status_has_something_to_show(fake, capsys):
    from gcx import profiles
    c = steps.Ctx("deltaai", profiles.load("deltaai"), {"keepalive": {"mode": "on-use"}},
                  None, menu.Prompter(True))
    steps.offer_globus_cli(c, assume_yes=True)  # no collection: silent
    c = steps.Ctx("delta", profiles.load("delta"), {"keepalive": {"mode": "on-use"}},
                  None, menu.Prompter(True))
    steps.offer_globus_cli(c, assume_yes=True)  # no cron state to show: silent
    assert capsys.readouterr().out == ""
    c = steps.Ctx("midway3", profiles.load("midway3"), {"keepalive": {"mode": "failover"}},
                  None, menu.Prompter(True))
    steps.offer_globus_cli(c, assume_yes=True)  # --yes: a hint, no prompt
    assert "`gcx login globus` makes `gcx status midway3`" in capsys.readouterr().out
    (fake.state / "logged-in").touch()
    (fake.state / f"ok-{MIDWAY3}").touch()
    steps.offer_globus_cli(c, assume_yes=True)
    out = capsys.readouterr().out
    assert "globus-cli" in out and "`gcx status midway3` (which login node holds the endpoint) works" in out
