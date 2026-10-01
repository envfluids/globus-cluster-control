"""`never_sent` decides whether a failed submit may be retried without risking
a duplicate run. Each case provokes a real failure over a local socket, then
wraps it the way globus_sdk does, so the test tracks requests/urllib3 changes.
"""

import socket
import threading

import pytest
import requests
from globus_sdk.exc import convert_request_exception

from globus_cluster_control.cli import never_sent, transient


def _server(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        handler(conn)

    threading.Thread(target=run, daemon=True).start()
    return f"127.0.0.1:{srv.getsockname()[1]}", srv


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _failure(url, **kw):
    try:
        requests.post(url, data=b"x", timeout=kw.pop("timeout", 2), **kw)
    except requests.RequestException as e:
        return convert_request_exception(e)
    pytest.fail("request unexpectedly succeeded")


def _read_then(conn, then):
    conn.recv(65536)
    then(conn)


def test_connection_refused_was_never_sent():
    e = _failure(f"http://127.0.0.1:{_free_port()}/")
    assert transient(e) and never_sent(e)


def test_unreachable_proxy_was_never_sent():
    # What tests/flaky_proxy.py does while "down": refuse connections.
    e = _failure("https://example.invalid/", proxies={"https": f"http://127.0.0.1:{_free_port()}"})
    assert transient(e) and never_sent(e)


def test_proxy_dropping_tunnel_is_ambiguous():
    # requests reports this exactly like a server hanging up after reading the
    # request, so it must be treated as possibly sent.
    addr, _ = _server(lambda c: _read_then(c, lambda c: c.close()))
    e = _failure("https://example.invalid/", proxies={"https": f"http://{addr}"})
    assert transient(e) and not never_sent(e)


def test_tls_error_is_ambiguous():
    # urllib3 raises the same SSLError for a failed handshake and for a
    # connection lost mid-response, so a TLS error cannot prove "unsent".
    addr, _ = _server(lambda c: _read_then(c, lambda c: (c.sendall(b"not tls\r\n\r\n"), c.close())))
    e = _failure(f"https://{addr}/")
    assert transient(e) and not never_sent(e)


def test_reset_after_request_is_ambiguous():
    # Server read the whole request, then hung up: it may have acted on it.
    addr, _ = _server(lambda c: _read_then(c, lambda c: c.close()))
    e = _failure(f"http://{addr}/")
    assert transient(e) and not never_sent(e)


def test_read_timeout_is_ambiguous():
    addr, _ = _server(lambda c: _read_then(c, lambda c: threading.Event().wait(5)))
    e = _failure(f"http://{addr}/", timeout=0.5)
    assert transient(e) and not never_sent(e)
