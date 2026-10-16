"""
A Subsonic call carries the user (``u``), the salted token (``t``), the salt (``s``) and sometimes the password (``p``)
in its query string, and httpx puts that URL in the message of an ``HTTPStatusError`` and in its own log line. None of
it may reach an exception text, a traceback or a log, and the failure must still be classified as it was.

The requests go through a real httpx client on a mock transport, so the errors and the "HTTP Request" log lines are
httpx's own.
"""
from __future__ import annotations

import logging
import traceback

import httpx
import pytest

import musizerr_provider_navidrome as module
from helpers import RemoteFailure, via_runner
from musizerr_provider_navidrome import NavidromeProvider, _redact
from musizerr_sdk.auth import AuthFlow, AuthSession
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.failures import classify_exception

from subsonic import song

USER = "alice-account-name"
PASSWORD = "hunter2-secret-password"
SETTINGS = {"url": "http://navi.test:4533", "mode": "full"}
ACCOUNT = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": USER, "password": PASSWORD})
KINDS = {401: "auth_failed", 404: "not_found", 429: "rate_limited", 500: "upstream_error", 502: "upstream_error"}


class Server:
    """A Subsonic server on a mock transport that answers every request with ``status`` (or raises ``error``), and
    remembers the credentials it was sent."""

    def __init__(self, monkeypatch, status: int = 200, error: Exception | None = None, body: dict | None = None):
        self.status, self.error = status, error
        self.body = body if body is not None else {"subsonic-response": {"status": "ok"}}
        self.sent: list[dict] = []
        monkeypatch.setattr(module.httpx, "get", self.get)
        monkeypatch.setattr(module.httpx, "stream", self.stream)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(dict(request.url.params))
        if self.error is not None:
            raise self.error.__class__(f"{self.error} {request.url}", request=request)
        return httpx.Response(self.status, json=self.body, headers={"content-type": "application/json"}, request=request)

    def get(self, url, params=None, timeout=None, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(self._handler)) as client:
            return client.get(url, params=params)

    def stream(self, method, url, timeout=None, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self._handler)).stream(method, url)

    @property
    def secrets(self) -> set[str]:
        """Every credential value that was on the wire (user, token, salt) and the password behind them."""
        found = {PASSWORD, USER}
        for params in self.sent:
            found |= {params[key] for key in ("t", "s", "p") if key in params}
        assert len(found) > 3, "the server saw no token and no salt"
        return found


def provider() -> NavidromeProvider:
    return NavidromeProvider(StaticProviderContext(settings=SETTINGS, provider_name="navidrome"))


def everything_said_about(exc: BaseException) -> str:
    """Every text an error can show: str, repr, the full traceback with its chain, the URLs it holds."""
    request = getattr(exc, "request", None) if not isinstance(exc, httpx.HTTPStatusError) else exc.request
    parts = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    if request is not None:
        parts.append(str(request.url))
    response = getattr(exc, "response", None)
    if response is not None:
        parts += [str(response.url), repr(response), repr(response.request)]
    return "\n".join(parts)


def assert_clean(text: str, server: Server) -> None:
    for secret in server.secrets:
        assert secret not in text, f"{secret!r} is in: {text[:300]}"


CALLS = {
    "a browse call": lambda p: p.search_tracks("anything", ACCOUNT),
    "a playlist call": lambda p: p.create_playlist("Drive", ["s1"], ACCOUNT),
    "a login check": lambda p: p.identify_account(ACCOUNT),
    "a library listing": lambda p: p.list_library(ACCOUNT),
}


@pytest.mark.parametrize("status", sorted(KINDS))
@pytest.mark.parametrize("call", sorted(CALLS))
def test_a_failing_call_keeps_its_class_status_and_kind_and_loses_the_credentials(monkeypatch, call, status):
    server = Server(monkeypatch, status=status)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        CALLS[call](provider())

    error = raised.value
    assert type(error) is httpx.HTTPStatusError and error.response.status_code == status
    assert classify_exception(error).kind == KINDS[status]
    assert "/rest/" in str(error) and str(status) in str(error)      # still says what failed
    assert_clean(everything_said_about(error), server)


def test_a_network_error_keeps_its_class_and_kind_and_loses_the_credentials(monkeypatch):
    server = Server(monkeypatch, error=httpx.ConnectError("connection refused"))

    with pytest.raises(httpx.ConnectError) as raised:
        provider().search_tracks("anything", ACCOUNT)

    assert classify_exception(raised.value).kind == "unreachable" and "connection refused" in str(raised.value)
    assert_clean(everything_said_about(raised.value), server)


def test_the_connection_test_reports_a_failure_without_the_credentials(monkeypatch):
    server = Server(monkeypatch, status=401)

    message = provider().test_connection(ACCOUNT)

    assert "401" in message and "/rest/ping" in message
    assert_clean(message, server)


def test_the_partial_download_logs_its_failure_without_the_credentials(monkeypatch, caplog, tmp_path):
    server = Server(monkeypatch, status=500)
    caplog.set_level(logging.DEBUG)

    assert provider().fetch_partial("song-1", str(tmp_path), ACCOUNT) is None

    assert "fetch_partial" in caplog.text and "500" in caplog.text     # the failure was logged
    assert_clean(caplog.text, server)


def test_no_log_line_of_a_working_or_failing_session_has_the_credentials(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="httpx")
    server = Server(monkeypatch, status=200, body={"subsonic-response": {"status": "ok", "searchResult3": {"song": [song(1)]}}})
    p = provider()
    p.search_tracks("anything", ACCOUNT)
    p.like_track("song-1", ACCOUNT)
    server.status = 500
    for call in CALLS.values():
        with pytest.raises(httpx.HTTPError):
            call(p)
    p.fetch_partial("song-1", str(tmp_path), ACCOUNT)

    assert "HTTP Request" in caplog.text, "httpx's own log line was not exercised"   # the filter is what keeps it clean
    assert_clean(caplog.text, server)


def test_a_password_parameter_is_redacted_too(monkeypatch):
    server = Server(monkeypatch, status=500)
    encoded = "enc:" + b"old-style-password".hex()      # how a Subsonic client sends `p`

    with pytest.raises(httpx.HTTPStatusError) as raised:
        provider()._api("ping", ACCOUNT, {"p": encoded})

    assert b"old-style-password".hex() not in everything_said_about(raised.value)
    assert_clean(everything_said_about(raised.value), server)


def test_through_the_runner_the_host_gets_the_same_kind_and_a_clean_message(monkeypatch):
    server = Server(monkeypatch, status=401)

    with pytest.raises(RemoteFailure) as failed:
        via_runner(NavidromeProvider, "search_tracks", "anything", ACCOUNT, settings=SETTINGS, provider_name="navidrome")

    assert failed.value.kind == "auth_failed" and failed.value.type_name == "HTTPStatusError"
    assert_clean(str(failed.value), server)


@pytest.mark.parametrize("text, kept", [
    ("Client error '401 Unauthorized' for url 'http://h:4533/rest/ping?u=alice&t=abc123&s=salt&v=1.16.1&c=musizerr&f=json'",
     ["/rest/ping", "v=1.16.1", "c=musizerr", "f=json"]),
    ("GET http://h/rest/stream?id=song-1&u=bob%40example.org&t=ff00&s=aa11 failed", ["id=song-1", "failed"]),
    ("http://h/rest/ping?p=enc:7061&u=a", ["/rest/ping"]),
    ("headers {'Authorization': 'Bearer sekret-token'}", ["headers"]),
    ("Authorization: Basic c2VjcmV0", []),
    ("size=300&songCount=20&query=u2", ["size=300", "songCount=20", "query=u2"]),   # other parameters are not touched
])
def test_the_redaction(text, kept):
    cleaned = _redact(text)

    for secret in ("alice", "abc123", "salt", "bob", "ff00", "aa11", "7061", "sekret-token", "c2VjcmV0"):
        assert secret not in cleaned
    for part in kept:
        assert part in cleaned
