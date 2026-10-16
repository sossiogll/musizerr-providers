"""
The host's side of scripts/integration.sh: the application's own code (``ProviderRegistry``, the host contexts, the
settings table, the workspaces, the grants) against the REAL gateway and the REAL images of the eight providers of this
repository, installed from the catalog scripts/build.sh wrote. It prints a marker line for each thing it proves and exits
non-zero on the first one that is wrong:

  DESCRIBE_OK     every provider is running in the gateway and the host describes it, with the roles and the permissions
                  the provider declares and its image labels carry; the registry rejected nothing;
  LYRICS_OK       LRCLIB gives lyrics (a real call to lrclib.net);
  MUSICBRAINZ_OK  MusicBrainz finds an artist and lists its albums (two real calls, spaced for its rate limit);
  LOCAL_OK        local storage stores, finds, measures, streams, lists and deletes a file in a granted folder, and the
                  file is on the host;
  FTP_OK          FTP storage does the same against a throwaway FTP server, in passive mode, through the proxy, with the
                  data connection on a port the server chose;
  ACOUSTID_OK     fpcalc runs inside the sandbox (a WAV is fingerprinted) and the lookup reaches api.acoustid.org on
                  port 443 (HTTPS); an invalid key is told apart; with no key it asks nobody;
  YOUTUBE_OK      the Data API is reachable and a call without a valid token fails as "reconnect";
  NAVIDROME_OK, LIDARR_OK   an unreachable or unconfigured server is a clean, classified failure and the provider keeps
                  running;
  GATEWAY_OK      every provider is still running at the end and the gateway refused none of their connections.

It is run in a container of the application's image, with the code of the main repository (read only) and the volumes of
the gateway. The main repository still holds the first-party providers in app/providers/: the run copies app/ and removes
those eight, which is how the registry will be once they are gone (a registry refuses a gateway provider whose name an
in-process one has).
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import time
import wave
from pathlib import Path

os.environ.setdefault("DATA_DIR", "/tmp/data")

from db.engine import engine, init_db  # noqa: E402
from sqlmodel import Session  # noqa: E402

from core.settings_resolver import namespace_provider_key  # noqa: E402
from db.settings_store import SettingsRepository  # noqa: E402
from musizerr_sdk.auth import AuthFlow, AuthSession  # noqa: E402
from musizerr_sdk.models import Album, Track  # noqa: E402
from musizerr_sdk.permissions import FilesystemAccess, Permissions, TcpAccess, canonical, to_json  # noqa: E402
from services.config_manager import get_config  # noqa: E402
from services.gateway_client import GatewayClient  # noqa: E402
from services.provider_proxy import ProviderCallFailed  # noqa: E402
from services.provider_registry import ProviderRegistry  # noqa: E402
from services.workspace import new_workspace  # noqa: E402

ANONYMOUS = AuthSession.anonymous()
LIBRARY = Path("/downloads/mzp52-library")

# What each provider declares: the roles it fills and the permissions its image carries. Written out here on purpose,
# apart from the providers' own code and tests.
EXPECTED: dict[str, tuple[set[str], Permissions]] = {
    "lrclib": ({"lyrics"}, Permissions(egress=("lrclib.net:443",))),
    "musicbrainz": ({"metadata"}, Permissions(egress=("musicbrainz.org:443",))),
    "youtube": ({"metadata"}, Permissions(egress=("www.googleapis.com:443",))),
    "lidarr": ({"download"}, Permissions(egress=("musicbrainz.org:443",), egress_from_settings=("url",))),
    "acoustid": ({"identification"}, Permissions(egress=("api.acoustid.org:443",))),
    "ftp": ({"storage"}, Permissions(tcp_from_settings=(TcpAccess("host", "any"),))),
    "local": ({"storage"}, Permissions(filesystem_from_settings=(FilesystemAccess("root_dir", "rw"),))),
    "navidrome": ({"metadata", "sync"}, Permissions(egress_from_settings=("url",))),
}


def say(marker: str, **fields) -> None:
    print(marker, json.dumps(fields, default=str) if fields else "", flush=True)


def fail(message: str) -> None:
    print("FAILED:", message, flush=True)
    sys.exit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def set_setting(provider: str, key: str, value: str) -> None:
    with Session(engine) as session:
        SettingsRepository(session).set_system(namespace_provider_key(provider, key), value)
        session.commit()
    get_config()._cache_loaded_at = 0.0  # noqa: SLF001 — read the settings now, not after the cache window


def listing() -> dict:
    return GatewayClient().request({"op": "list"})


def provider_state(name: str) -> dict:
    (found,) = [p for p in listing()["providers"] if p["name"] == name]
    return found


def finds(registry: ProviderRegistry, name: str):
    """The provider ``name`` under each role it fills, or the roles it is registered under."""
    return {
        role: getter(name)
        for role, getter in (
            ("metadata", registry.get_metadata_provider), ("download", registry.get_download_provider),
            ("lyrics", registry.get_lyrics_provider), ("storage", registry.get_storage_provider),
            ("sync", registry.get_sync_provider), ("identification", registry.get_identification_provider),
        ) if getter(name) is not None
    }


def failing(call, what: str):
    """The ``ProviderCallFailed`` (or contract exception) a call raises; fail if it does not raise."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001
        return exc
    fail(f"{what} did not fail")


def clean_failure(exc: Exception, what: str) -> dict:
    expect(isinstance(exc, ProviderCallFailed) and str(exc), f"{what}: not a classified failure: {type(exc).__name__}: {exc}")
    return {"kind": exc.kind, "type": exc.exception_type, "message": str(exc)[:100]}


# ------------------------------------------------------------------


def check_describe(registry: ProviderRegistry) -> None:
    expect(registry.rejected() == {}, f"the registry rejected providers: {registry.rejected()}")
    report = {}
    for name, (roles, permissions) in EXPECTED.items():
        under = finds(registry, name)
        expect(set(under) == roles, f"{name} is registered under {sorted(under)}, expected {sorted(roles)}")
        proxy = next(iter(under.values()))
        expect(canonical(proxy.manifest.permissions) == canonical(permissions), f"{name}: the host describes it with {proxy.manifest.permissions}")
        expect(proxy.manifest.provider_api_version == 5 and proxy.manifest.name == name, f"{name}: {proxy.manifest.provider_api_version}")
        state = provider_state(name)
        expect(state["state"] == "running", f"{name} is {state['state']}: {state.get('reason')}")
        expect(json.loads(to_json(permissions)) == state["declared"]["permissions"] == json.loads(to_json(proxy.manifest.permissions)),
               f"{name}: the gateway's declaration differs: {state['declared']['permissions']}")
        report[name] = {"roles": sorted(under), "label": proxy.label, "permissions": json.loads(to_json(permissions))}
    say("DESCRIBE_OK", providers=sorted(report), rejected=registry.rejected())
    for name, item in report.items():
        print("   ", name, json.dumps(item), flush=True)


def check_lyrics(registry: ProviderRegistry) -> None:
    lyrics = registry.get_lyrics_provider("lrclib").get_lyrics(
        Track(title="Bohemian Rhapsody", artist="Queen", album="A Night at the Opera", duration_ms=354_000), ANONYMOUS,
    )
    expect(lyrics is not None and lyrics.source == "lrclib" and lyrics.synced and "real life" in (lyrics.plain or ""), f"no lyrics: {lyrics!r}")
    say("LYRICS_OK", source=lyrics.source, plain_chars=len(lyrics.plain or ""), synced_lines=len(lyrics.synced))


def patiently(call, what: str, attempts: int = 4):
    """A public service that answers 503 or 429 (MusicBrainz does, to a shared address, whatever the rate we keep) is asked
    again a few times, with a pause; any other failure is a failure."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ProviderCallFailed as exc:
            if exc.kind not in ("upstream_error", "rate_limited") or attempt == attempts:
                fail(f"{what}: {exc}")
            say("RETRY", what=what, attempt=attempt, kind=exc.kind)
            time.sleep(5 * attempt)


def check_musicbrainz(registry: ProviderRegistry) -> None:
    musicbrainz = registry.get_metadata_provider("musicbrainz")
    artists = patiently(lambda: musicbrainz.search_artists("Radiohead", ANONYMOUS, limit=3), "MusicBrainz search")
    expect(artists and any(a.name == "Radiohead" for a in artists), f"MusicBrainz found no Radiohead: {artists!r}")
    radiohead = next(a for a in artists if a.name == "Radiohead")
    time.sleep(1.2)  # MusicBrainz asks for at most one request a second
    albums = patiently(lambda: musicbrainz.get_artist_albums(radiohead.provider_id, ANONYMOUS, limit=5), "MusicBrainz albums")
    expect(albums and all(isinstance(a, Album) and a.title for a in albums), f"no albums: {albums!r}")
    say("MUSICBRAINZ_OK", artist=radiohead.name, mbid=radiohead.provider_id, albums=[a.title for a in albums])


def workspace_file(provider: str, name: str, data: bytes):
    workspace = new_workspace(get_config().workspace_dir, provider)
    (workspace.path / name).write_bytes(data)
    return workspace, workspace.file(name)


def check_storage(registry: ProviderRegistry, provider_name: str, session: AuthSession) -> dict:
    """store / exists / size / stream (whole and from an offset) / list / delete against a storage provider."""
    storage = registry.get_storage_provider(provider_name)
    audio = bytes(range(256)) * 1024  # 256 KiB
    relative = "Some Artist/Some Album/01 Some Song.flac"
    work, source = workspace_file(provider_name, "song.flac", audio)
    try:
        expect(storage.exists(relative, session) is False, f"{provider_name}: the file exists before it is stored")
        expect(storage.store(source, relative, session) == relative, f"{provider_name}: store answered another path")
        expect(storage.exists(relative, session) is True, f"{provider_name}: the stored file is not found")
        expect(storage.size(relative, session) == len(audio), f"{provider_name}: size {storage.size(relative, session)}")
        expect(storage.test_connection(session) is None, f"{provider_name}: {storage.test_connection(session)}")

        out = new_workspace(get_config().workspace_dir, provider_name)
        try:
            streamed = storage.stream(relative, str(out.path), session)
            if streamed.file:
                got = Path(out.file(streamed.file).path).read_bytes()
            else:
                got = b"".join(streamed.chunks)
            expect(got == audio, f"{provider_name}: the streamed bytes differ ({len(got)} of {len(audio)})")
            expect(streamed.media_type == "audio/flac", f"{provider_name}: media type {streamed.media_type}")
            seeked = None
            if getattr(storage, "supports_stream_offset", False):
                part = storage.stream(relative, str(out.path), session, offset=100_000)
                seeked = b"".join(part.chunks)
                expect(seeked == audio[100_000:] and part.offset == 100_000, f"{provider_name}: the seek from 100000 gave {len(seeked)} bytes")
        finally:
            out.cleanup()
        listed = sorted(storage.list_files("", session))
        expect(listed == [relative], f"{provider_name}: listing {listed}")
        expect(storage.delete(relative, session) is True and storage.exists(relative, session) is False, f"{provider_name}: delete")
    finally:
        work.cleanup()
    return {"bytes": len(audio), "listed": listed, "seek_bytes": None if seeked is None else len(seeked)}


def check_local(registry: ProviderRegistry) -> None:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    LIBRARY.chmod(0o777)
    set_setting("local", "root_dir", str(LIBRARY))
    storage = registry.get_storage_provider("local")
    # Until the folder is granted the sandbox has none of it; the first call carries the grant (and restarts the sandbox).
    started = time.monotonic()
    outcome = check_storage(registry, "local", ANONYMOUS)
    # The file went through the sandbox to the host's disk: store it once more and look at the folder from here.
    work, source = workspace_file("local", "again.flac", b"on the host")
    try:
        storage.store(source, "Host/Check/again.flac", ANONYMOUS)
    finally:
        work.cleanup()
    on_host = (LIBRARY / "Host" / "Check" / "again.flac").read_bytes()
    expect(on_host == b"on the host", "the file written from the sandbox is not on the host")
    storage.delete("Host/Check/again.flac", ANONYMOUS)
    refused = provider_state("local").get("folders", {}).get("refused")
    expect(not refused, f"the gateway refused the folder: {refused}")
    say("LOCAL_OK", folder=str(LIBRARY), seconds=round(time.monotonic() - started, 1), **outcome)


def check_ftp(registry: ProviderRegistry) -> None:
    set_setting("ftp", "host", "ftp-server")
    set_setting("ftp", "port", "2121")
    set_setting("ftp", "remote_base_path", "/music")
    session = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "musizerr", "password": "synthetic-password"})
    outcome = check_storage(registry, "ftp", session)
    say("FTP_OK", server="ftp-server:2121", **outcome)


def tone(path: Path, seconds: int = 8, rate: int = 22_050) -> None:
    frames = b"".join(
        struct.pack("<h", int(12_000 * math.sin(2 * math.pi * (440 + 20 * math.sin(n / rate * 3)) * n / rate)))
        for n in range(seconds * rate)
    )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


def check_acoustid(registry: ProviderRegistry) -> None:
    acoustid = registry.get_identification_provider("acoustid")
    workspace = new_workspace(get_config().workspace_dir, "acoustid")
    try:
        tone(workspace.path / "tone.wav")
        audio = workspace.file("tone.wav")
        # No key: inert, asks nobody (and does not even fingerprint).
        expect(acoustid.is_available() is False, "acoustid is available without a key")
        expect(acoustid.identify(audio) is None, "acoustid identified something without a key")
        # A key the service does not know: fpcalc fingerprints the WAV inside the sandbox, the lookup goes out to
        # api.acoustid.org:443 through the proxy, and the answer is told apart from a failure of the network.
        set_setting("acoustid", "api_key", "synthetic-key-not-registered")
        expect(acoustid.is_available() is True, "acoustid is not available with a key")
        verdict = acoustid.test_connection(ANONYMOUS)
        expect(verdict == "API key AcoustID non valida", f"test_connection said {verdict!r}")
        identity = acoustid.identify(audio)
        expect(identity is None, f"a synthetic tone was identified: {identity!r}")
    finally:
        workspace.cleanup()
    say("ACOUSTID_OK", test_connection=verdict, identity=None)


def check_youtube(registry: ProviderRegistry) -> None:
    youtube = registry.get_metadata_provider("youtube")
    token = AuthSession.oauth2(headers={"Authorization": "Bearer synthetic-token-not-valid"})
    error = failing(lambda: youtube.get_track("synthetic-video-id", token), "a call with an invalid token")
    expect(isinstance(error, PermissionError) and "reconnect YouTube" in str(error), f"YouTube: {type(error).__name__}: {error}")
    expect(youtube.search_tracks("anything", token) == [], "youtube searched something")
    say("YOUTUBE_OK", failure=type(error).__name__, message=str(error)[:80])


def check_navidrome(registry: ProviderRegistry) -> None:
    navidrome = registry.get_metadata_provider("navidrome")
    session = AuthSession(flow=AuthFlow.CREDENTIALS, extra={"username": "alice", "password": "synthetic-password"})
    set_setting("navidrome", "mode", "full")
    # Configured, but nothing listens there: the server is on the network of the stack, the port is closed.
    set_setting("navidrome", "url", "http://ftp-server:4533")
    error = failing(lambda: navidrome.search_tracks("anything", session), "a search on a closed port")
    unreachable = clean_failure(error, "navidrome")
    message = navidrome.test_connection(session)
    expect(isinstance(message, str) and message, f"the connection test said {message!r}")
    # The Subsonic credentials (user, token, salt, password) are in no text the host gets of the failure.
    for text in (str(error), message):
        expect("/rest/" in text and "502" in text, f"the failure says nothing useful: {text[:200]}")
        expect("alice" not in text and "synthetic-password" not in text and not re.search(r"[?&](?:u|t|s|p|username)=(?!redacted)", text),
               f"Subsonic credentials in a failure text: {text[:300]}")
    say("NAVIDROME_REDACTED_OK", error=str(error).split("\n")[0][:230])
    # Not configured: no address at all.
    set_setting("navidrome", "url", "")
    unconfigured = clean_failure(failing(lambda: navidrome.list_library(session), "a listing with no URL"), "navidrome")
    expect(provider_state("navidrome")["state"] == "running", "navidrome stopped running")
    say("NAVIDROME_OK", unreachable=unreachable, test_connection=message[:100], unconfigured=unconfigured)


def check_lidarr(registry: ProviderRegistry) -> None:
    lidarr = registry.get_download_provider("lidarr")
    set_setting("lidarr", "url", "http://ftp-server:8686")
    session = AuthSession(flow=AuthFlow.API_KEY, params={"apikey": "synthetic"}, extra={"url": "http://ftp-server:8686"})
    message = lidarr.test_connection(session)
    expect(isinstance(message, str) and "unreachable" in message, f"the connection test said {message!r}")
    polled = clean_failure(failing(lambda: lidarr.poll_delegation("1", session), "a poll of an unreachable Lidarr"), "lidarr")
    expect(polled["type"] == "LidarrUnreachable", f"the host should hear LidarrUnreachable: {polled}")
    none = AuthSession(flow=AuthFlow.API_KEY, params={"apikey": "synthetic"}, extra={})
    expect(lidarr.test_connection(none) == "Lidarr URL not configured", "an unconfigured Lidarr said something else")
    expect(lidarr.supports_fuzzy is True and lidarr.enabled_by_default is False, "lidarr declarations")
    say("LIDARR_OK", test_connection=message[:100], poll=polled)


def check_gateway() -> None:
    document = listing()
    states = {p["name"]: p["state"] for p in document["providers"]}
    expect(set(states) == set(EXPECTED) and set(states.values()) == {"running"}, f"the gateway runs: {states}")
    refusals = {p["name"]: p.get("egress", {}).get("refused") for p in document["providers"] if p.get("egress", {}).get("refused")}
    expect(not refusals, f"the gateway refused connections of the providers: {refusals}")
    say("GATEWAY_OK", states=states, restarts={p["name"]: p.get("restarts", 0) for p in document["providers"]})


def main() -> None:
    init_db()
    registry = ProviderRegistry()
    check_describe(registry)
    check_lyrics(registry)
    check_musicbrainz(registry)
    check_local(registry)
    check_ftp(registry)
    check_acoustid(registry)
    check_youtube(registry)
    check_navidrome(registry)
    check_lidarr(registry)
    check_gateway()


if __name__ == "__main__":
    main()
