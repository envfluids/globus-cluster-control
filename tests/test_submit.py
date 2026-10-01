"""`run_at_most_once` must retry a provably-unsent request and must not retry one that
may have been delivered. Uses a stand-in Client whose `run` raises real,
socket-produced Globus errors (see test_never_sent.py), so no network or
Globus account is involved.
"""

import contextlib
import types

import pytest
import requests
from globus_sdk.exc import convert_request_exception

from gcx import transport
from test_never_sent import _failure, _free_port, _read_then, _server


class FakeClient:
    def __init__(self, failures):
        self.failures = list(failures)
        self.run_calls = 0
        self.max_retries_during_run = []
        cfg = types.SimpleNamespace(max_retries=5)

        @contextlib.contextmanager
        def tune(max_retries=None):
            saved, cfg.max_retries = cfg.max_retries, max_retries
            yield
            cfg.max_retries = saved

        cfg.tune = tune
        session = requests.Session()
        self._compute_web_client = types.SimpleNamespace(v3=types.SimpleNamespace(
            retry_config=cfg, transport=types.SimpleNamespace(session=session)))
        self._cfg = cfg

    def register_function(self, fn):
        return "fid"

    def run(self, **kwargs):
        self.run_calls += 1
        self.max_retries_during_run.append(self._cfg.max_retries)
        if self.failures:
            raise self.failures.pop(0)
        return "task-id"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)


def refused():
    return _failure(f"http://127.0.0.1:{_free_port()}/")


def reset_after_request():
    addr, _ = _server(lambda c: _read_then(c, lambda c: c.close()))
    return _failure(f"http://{addr}/")


def test_unsent_failures_are_retried_until_success():
    c = FakeClient([refused(), refused()])
    assert transport.run_at_most_once(c, "ep", "fid", cmd="sbatch x") == "task-id"
    assert c.run_calls == 3
    assert c.max_retries_during_run == [0, 0, 0]  # SDK's own retries were off


def test_possibly_delivered_failure_is_not_retried():
    c = FakeClient([reset_after_request()])
    with pytest.raises(transport.AmbiguousSubmission):
        transport.run_at_most_once(c, "ep", "fid", cmd="sbatch x")
    assert c.run_calls == 1


def test_unsent_then_ambiguous_stops_at_the_ambiguous_one():
    c = FakeClient([refused(), reset_after_request(), refused()])
    with pytest.raises(transport.AmbiguousSubmission):
        transport.run_at_most_once(c, "ep", "fid", cmd="sbatch x")
    assert c.run_calls == 2


def test_sdk_retries_restored_after_submit():
    c = FakeClient([])
    transport.run_at_most_once(c, "ep", "fid", cmd="hostname")
    assert c._cfg.max_retries == 5
