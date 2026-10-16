"""
What a provider's declared permissions allow, asked the way the gateway's egress proxy asks it.

The same module is in the tests of every provider of this repository (they are independent packages and
share nothing); keep the copies identical.

``allowed(permissions, url_or_target, settings)`` is True when a destination is one the permissions grant:

- a FIXED destination (``egress``, wildcards included), or
- one the CURRENT value of a setting named by ``egress_from_settings`` gives (a URL: its host and port,
  its scheme's default port when it names none), or
- a host a setting of ``tcp_from_settings`` gives, on a port its declaration covers.

``record_http(monkeypatch)`` replaces the ``httpx`` request functions with a recorder that answers every
request with an empty JSON object, so a test can run a provider's methods and read WHICH destinations it asked
for, the evidence that the declaration covers what the code does.
"""
from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit

from musizerr_sdk.permissions import (
    DEFAULT_PORTS,
    Permissions,
    is_wildcard,
    normalize_host,
    parse_egress,
    parse_ports,
    targets_from_value,
    wildcard_matches,
)


def destination(value: str) -> tuple[str, int]:
    """``(host, port)`` of a URL or of ``host:port``."""
    if "://" in value:
        parts = urlsplit(value)
        port = parts.port or DEFAULT_PORTS[parts.scheme.lower()]
        return normalize_host(parts.hostname or ""), port
    host, _, port = value.rpartition(":")
    return normalize_host(host), int(port)


def allowed(permissions: Permissions, target: str, settings: dict | None = None) -> bool:
    host, port = destination(target)
    for entry in permissions.egress:
        fixed_host, fixed_port = parse_egress(entry)
        if fixed_port == port and (wildcard_matches(fixed_host, host) if is_wildcard(fixed_host) else fixed_host == host):
            return True
    values = settings or {}
    for key in permissions.egress_from_settings:
        if (host, port) in targets_from_value(values.get(key)):
            return True
    for access in permissions.tcp_from_settings:
        first, last = parse_ports(access.ports)
        if host in {named for named, _ in targets_from_value(values.get(access.setting))} and first <= port <= last:
            return True
    return False


class Recorder:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def answer(self, method: str, url: str):
        import httpx

        self.urls.append(str(url))
        return httpx.Response(200, json={}, request=httpx.Request(method, str(url)))


def record_http(monkeypatch) -> Recorder:
    import httpx

    recorder = Recorder()
    for method in ("get", "post", "put", "delete"):
        monkeypatch.setattr(httpx, method, lambda url, *a, _m=method.upper(), **kw: recorder.answer(_m, url))

    @contextmanager
    def stream(method, url, *args, **kwargs):
        yield recorder.answer(method, url)

    monkeypatch.setattr(httpx, "stream", stream)
    return recorder


def attempt(*calls) -> None:
    """Run each call and ignore what it raises: a provider handed an empty answer may well fail on it, and
    only the requests it made matter here."""
    for call in calls:
        try:
            call()
        except Exception:  # noqa: BLE001
            pass
