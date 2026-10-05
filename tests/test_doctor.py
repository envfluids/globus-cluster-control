"""`gcx doctor` with no cluster: every configured cluster, one line each."""

from gcx import doctor

HEALTHY = [("endpoint online (Globus service)", True, "online"),
           ("endpoint answers", True, "login4, Python 3.12.9"),
           ("allowlist in force", True, "10 functions")]


def fake_checks(client, cluster):
    if cluster == "broken":
        raise ConnectionError("network unreachable")
    if cluster == "offline":
        yield ("endpoint online (Globus service)", False, "offline (keepalive: on-use)")
        return
    yield from HEALTHY


def test_summary_lines_and_failures_spelled_out(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "checks", fake_checks)
    rc = doctor.main_all(lambda: None, ["midway3", "offline", "broken"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "PASS  midway3  3 checks, endpoint on login4" in out
    assert "FAIL  offline  1 of 1 checks failed" in out and "offline (keepalive: on-use)" in out
    assert "FAIL  broken   1 of 1 checks failed" in out and "ConnectionError: network unreachable" in out
    assert "allowlist in force" not in out  # passing checks hidden without -v
    assert "1 of 3 clusters healthy" in out


def test_verbose_shows_every_check_and_all_healthy_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "checks", fake_checks)
    assert doctor.main_all(lambda: None, ["a", "b"], verbose=True) == 0
    out = capsys.readouterr().out
    assert out.count("allowlist in force") == 2 and "2 of 2 clusters healthy" in out


def test_no_clusters(capsys):
    assert doctor.main_all(lambda: None, []) == 1
    assert "no clusters configured" in capsys.readouterr().out
