from gcx import allowlist

BASE = "display_name: awikner midway3 login (pilot)\n"


def test_render_adds_sorted_block():
    out = allowlist.render(BASE, {"b-uuid", "a-uuid"})
    assert out == BASE + ("# gcx: only these registered functions may run (gcx allowlist).\n"
                          "allowed_functions:\n  - a-uuid\n  - b-uuid\n")


def test_render_replaces_existing_block_and_keeps_other_keys():
    old = allowlist.render(BASE + "public: false\n", {"old"})
    new = allowlist.render(old, {"new"})
    assert "old" not in new.split("allowed_functions:")[1] and "  - new" in new
    assert "public: false" in new and new.count("allowed_functions:") == 1


def test_render_off_restores_original():
    assert allowlist.render(allowlist.render(BASE, {"x"}), None) == BASE


def _run_apply(monkeypatch, mode):
    from gcx import config
    sent = []
    cfg = {"ssh": "c", "endpoint_name": "gcx", "remote_state": "~/.gcx/state",
           "remote_root": "~/.gcx", "endpoint": "ep", "keepalive": {"mode": mode}}
    monkeypatch.setattr(config, "load", lambda cluster: cfg)
    monkeypatch.setattr(allowlist, "wanted_uuids", lambda cluster, cfg: {"u1"})
    monkeypatch.setattr(allowlist.time, "sleep", lambda s: None)
    monkeypatch.setattr(allowlist, "_ssh", lambda alias, cmd, stdin=None: sent.append(cmd) or BASE)
    states = iter([(False, set()), (True, {"u1"})])
    monkeypatch.setattr(allowlist, "service_state", lambda client, ep: next(states))
    assert allowlist.run(None, "c", apply=True) == 0
    return sent


def test_cron_cluster_restarts_through_the_keepalive(monkeypatch):
    sent = _run_apply(monkeypatch, "failover")
    assert any("touch ~/.gcx/state/restart-request" in c for c in sent)
    assert not any(" start --detach" in c for c in sent)


def test_cluster_without_keepalive_restarts_directly(monkeypatch):
    # Delta, 2026-10-02: a restart-request with no keepalive to honour it hung.
    sent = _run_apply(monkeypatch, "on-use")
    restart = [c for c in sent if " start --detach gcx" in c]
    assert restart and "rm -f ~/.gcx/state/restart-request" in restart[0]
    assert not any(c.startswith("touch") for c in sent)
