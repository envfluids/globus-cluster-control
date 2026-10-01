"""Talking to the Globus Compute service across an unreliable network.

Every call is a stateless HTTPS request, so a network drop costs a retry, not a
re-login. Read-only calls (version check, registration, polling for results)
are retried freely. Submission is at-most-once: it is retried only when the
request provably never left this machine; otherwise `AmbiguousSubmission` is
raised rather than risk running the task twice (a second `sbatch`).
"""

import sys
import time

import requests
import urllib3.exceptions
from globus_sdk import GlobusAPIError, NetworkError

EXIT_AMBIGUOUS = 75  # EX_TEMPFAIL: submission state unknown, check before resubmitting

# Statuses where the service answered without doing the work.
REJECTED_STATUSES = (429, 503)
# Statuses where it may or may not have done the work.
AMBIGUOUS_STATUSES = (500, 502, 504)


class AmbiguousSubmission(Exception):
    pass


def _requests_error(e):
    """The underlying requests exception of a Globus network error, if any."""
    while e is not None:
        if isinstance(e, requests.RequestException):
            return e
        e = getattr(e, "underlying_exception", None) or e.__cause__
    return None


def never_sent(e):
    """True only if the request certainly did not reach the server.

    Connection refused, DNS failure, connect timeout and an unreachable proxy
    all happen before a request byte is written. A reset or timeout after
    connecting may follow a fully delivered request -- and a proxy dropping its
    tunnel looks exactly like that, so it counts as sent. TLS errors count as
    sent too: urllib3 raises the same SSLError for a failed handshake and for a
    connection lost while reading the response.
    """
    if isinstance(e, GlobusAPIError):
        return e.http_status in REJECTED_STATUSES
    r = _requests_error(e)
    if isinstance(r, requests.ConnectTimeout):
        return True
    if isinstance(r, requests.ConnectionError) and r.args:
        reason = getattr(r.args[0], "reason", None)  # urllib3 MaxRetryError
        return isinstance(reason, (urllib3.exceptions.NewConnectionError,
                                   urllib3.exceptions.ProxyError))
    return False


def transient(e):
    return isinstance(e, NetworkError) or (
        isinstance(e, GlobusAPIError)
        and e.http_status in REJECTED_STATUSES + AMBIGUOUS_STATUSES)


def retry(fn, *args, safe=lambda e: True, attempts=30, **kwargs):
    """Call fn, retrying transient failures that `safe` allows (~10 min total)."""
    delay = 2.0
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not transient(e) or i == attempts - 1:
                raise
            if not safe(e):
                raise AmbiguousSubmission(f"{type(e).__name__}: {e}") from e
            print(f"[gcx] network error ({type(e).__name__}), retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


def run_at_most_once(client, endpoint_id, function_id, **kwargs):
    """Submit one task; raise AmbiguousSubmission if it may have been delivered."""
    # The SDK's transport retries any network error itself, POSTs included;
    # turn that off so `never_sent` sees every failure of the submit request.
    web = client._compute_web_client.v3
    with web.retry_config.tune(max_retries=0):
        return retry(_run_on_fresh_connection, client, web, endpoint_id=endpoint_id,
                     function_id=function_id, safe=never_sent, **kwargs)


def _run_on_fresh_connection(client, web, **kwargs):
    # A pooled keep-alive socket that died during an outage fails on write,
    # which is indistinguishable from a request that was delivered. A fresh
    # connection fails at connect instead, which is provably unsent.
    for adapter in web.transport.session.adapters.values():
        adapter.close()
    return client.run(**kwargs)


def wait(client, task_id, poll=2.0):
    while True:
        task = retry(client.get_task, task_id)
        if not task.get("pending", True):
            return task
        time.sleep(poll)
        poll = min(poll * 1.5, 15.0)
