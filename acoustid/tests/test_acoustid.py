"""AcoustIDProvider: validate_config e test_connection devono riflettere la
realtà (Fase 5b fix 4). Prima di questo fix ``validate_config`` era sempre
``True`` (config_valid mentiva) e ``test_connection`` si limitava a
controllare che la libreria fosse importabile e la chiave non vuota — non
contattava mai il servizio, quindi una chiave palesemente sbagliata risultava
"connessa" tanto quanto una vera.

Il servizio AcoustID distingue con un error code una API key non valida (4,
"invalid API key") da un fingerprint non valido (3, "invalid fingerprint"):
una richiesta minima e deliberatamente fittizia (nessun audio reale, quindi
niente fpcalc/chromaprint) è sufficiente per validare la SOLA chiave — un
code 3 vuol dire che la chiave È stata accettata. Verificato dal vivo con la
chiave reale dell'utente (non qui, per non mettere una chiave reale in un
fixture): una chiave inventata restituisce code 4 "invalid API key", la
chiave reale restituisce code 3 "invalid fingerprint" — cioè è valida.
"""

from __future__ import annotations

import math
import shutil
import struct
import wave
from pathlib import Path

import pytest
import requests

import musizerr_provider_acoustid as acoustid_module
from helpers import via_runner
from musizerr_sdk.interfaces import WorkspaceFile
from musizerr_sdk.context import StaticProviderContext
from musizerr_provider_acoustid import AcoustIDProvider


class _FakeAcoustidLib:
    """Sostituisce il modulo ``acoustid`` reale: nessuna rete nei test."""

    def __init__(self, response=None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[tuple] = []
        self.timeouts: list = []

    def lookup(self, api_key, fingerprint, duration, meta=None, timeout=None):
        self.calls.append((api_key, fingerprint, duration, meta))
        self.timeouts.append(timeout)
        if self._exc is not None:
            raise self._exc
        return self._response


def test_validate_config_requires_an_api_key():
    provider = AcoustIDProvider(StaticProviderContext())
    assert provider.validate_config({}) is False
    assert provider.validate_config({"api_key": ""}) is False
    assert provider.validate_config({"api_key": "abc"}) is True


def test_test_connection_without_library_reports_missing_dependency(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", False)
    provider = AcoustIDProvider(StaticProviderContext())

    assert provider.test_connection(session=object()) == "libreria pyacoustid non installata"


def test_test_connection_without_api_key_reports_missing_key(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    provider = AcoustIDProvider(StaticProviderContext())
    monkeypatch.setattr(provider, "_api_key", lambda: "")

    assert provider.test_connection(session=object()) == "API key AcoustID non configurata"


def test_test_connection_maps_invalid_api_key_error_code_to_a_clear_message(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    fake = _FakeAcoustidLib(response={"status": "error", "error": {"code": 4, "message": "invalid API key"}})
    monkeypatch.setattr(acoustid_module, "_acoustid", fake)
    provider = AcoustIDProvider(StaticProviderContext())
    monkeypatch.setattr(provider, "_api_key", lambda: "bogus")

    result = provider.test_connection(session=object())

    assert result == "API key AcoustID non valida"
    assert fake.calls, "deve davvero contattare il servizio, non limitarsi a controllare che la chiave non sia vuota"


def test_test_connection_treats_invalid_fingerprint_error_as_a_valid_key(monkeypatch):
    # Il servizio non ha modo di sapere che il fingerprint è fittizio: un
    # code 3 ("invalid fingerprint") con una chiave sintatticamente valida
    # vuol dire che la chiave stessa È stata accettata.
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    fake = _FakeAcoustidLib(response={"status": "error", "error": {"code": 3, "message": "invalid fingerprint"}})
    monkeypatch.setattr(acoustid_module, "_acoustid", fake)
    provider = AcoustIDProvider(StaticProviderContext())
    monkeypatch.setattr(provider, "_api_key", lambda: "real-key")

    assert provider.test_connection(session=object()) is None


def test_test_connection_reports_network_errors(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    fake = _FakeAcoustidLib(exc=RuntimeError("connection refused"))
    monkeypatch.setattr(acoustid_module, "_acoustid", fake)
    provider = AcoustIDProvider(StaticProviderContext())
    monkeypatch.setattr(provider, "_api_key", lambda: "real-key")

    result = provider.test_connection(session=object())

    assert result is not None and "connection refused" in result


def test_test_connection_reports_unexpected_error_codes_verbatim(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    fake = _FakeAcoustidLib(response={"status": "error", "error": {"code": 6, "message": "rate limit exceeded"}})
    monkeypatch.setattr(acoustid_module, "_acoustid", fake)
    provider = AcoustIDProvider(StaticProviderContext())
    monkeypatch.setattr(provider, "_api_key", lambda: "real-key")

    result = provider.test_connection(session=object())

    assert result is not None and "rate limit exceeded" in result


# ---------------------------------------------------------------------------
# is_available() risponde solo a "il provider può lavorare?" (libreria +
# API key). Se il sistema debba identificare (identification_enabled) lo
# decide l'host nel call site generico, vedi tests/test_tasks_anti_mislabel.py:
# il provider non ha accesso alle impostazioni dell'app.
# ---------------------------------------------------------------------------


def test_is_available_with_library_and_api_key(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    provider = AcoustIDProvider(StaticProviderContext())
    monkeypatch.setattr(provider, "_api_key", lambda: "real-key")
    assert provider.is_available() is True


def test_is_available_still_requires_library_and_api_key(monkeypatch):
    provider = AcoustIDProvider(StaticProviderContext())

    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", False)
    monkeypatch.setattr(provider, "_api_key", lambda: "real-key")
    assert provider.is_available() is False

    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    monkeypatch.setattr(provider, "_api_key", lambda: "")
    assert provider.is_available() is False


# ---------------------------------------------------------------------------
# identify() restituisce il miglior match col suo score grezzo: la soglia di
# confidenza la applica l'host (tests/test_identification_policy.py).
# ---------------------------------------------------------------------------


class _FakeMatchLib:
    def __init__(self, results):
        self._results = results

    def match(self, api_key, audio_path, timeout=None):
        self.timeout = timeout
        return iter(self._results)


def test_identify_returns_the_best_match_even_with_a_low_score(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    monkeypatch.setattr(acoustid_module, "_acoustid", _FakeMatchLib([
        (0.20, "rid-low", "Other", "Someone"),
        (0.35, "rid-best", "Song", "Artist"),
    ]))
    provider = AcoustIDProvider(StaticProviderContext(settings={"api_key": "key"}))

    identity = provider.identify(WorkspaceFile(workspace="/tmp", name="any.mp3"))

    assert identity is not None
    assert identity.recording_id == "rid-best"
    assert identity.score == 0.35


def test_identify_returns_none_when_the_best_match_has_no_title_or_artist(monkeypatch):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    monkeypatch.setattr(acoustid_module, "_acoustid", _FakeMatchLib([(0.99, "rid", None, "Artist")]))
    provider = AcoustIDProvider(StaticProviderContext(settings={"api_key": "key"}))

    assert provider.identify(WorkspaceFile(workspace="/tmp", name="any.mp3")) is None


# ---------------------------------------------------------------------------
# Added here: the real fingerprinting, the "no key" path and the runner
# ---------------------------------------------------------------------------

def _tone(path, seconds: int = 8, rate: int = 44_100) -> None:
    """A synthetic mono WAV: a 440 Hz tone with a slow wobble, so the fingerprint is not silence."""
    frames = b"".join(
        struct.pack("<h", int(12_000 * math.sin(2 * math.pi * (440 + 20 * math.sin(n / rate * 3)) * n / rate)))
        for n in range(seconds * rate)
    )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


class _Service:
    """The AcoustID web service: records the POSTs of pyacoustid, answers with one recording."""

    def __init__(self, monkeypatch, results=None):
        self.posts: list[tuple[str, dict]] = []
        self.results = results if results is not None else [
            {"id": "x", "score": 0.93, "recordings": [{"id": "rid-1", "title": "Tone", "artists": [{"name": "Sine"}]}]},
        ]
        service = self

        class _Reply:
            def json(self):
                return {"status": "ok", "results": service.results}

        def post(session, url, data=None, headers=None, timeout=None):
            self.posts.append((url, dict(data)))
            return _Reply()

        monkeypatch.setattr(requests.Session, "post", post)


def test_without_an_api_key_it_is_inert_and_asks_nobody(monkeypatch, tmp_path):
    service = _Service(monkeypatch)
    monkeypatch.setattr(acoustid_module._acoustid, "fingerprint_file", lambda *a, **k: pytest.fail("fingerprinted"))
    provider = AcoustIDProvider(StaticProviderContext(settings={}))

    assert provider.is_available() is False
    assert provider.identify(WorkspaceFile(workspace=str(tmp_path), name="tone.wav")) is None
    assert service.posts == []


@pytest.mark.skipif(shutil.which("fpcalc") is None, reason="fpcalc is not installed (scripts/test.sh installs it from tests/apt-packages.txt)")
def test_a_real_file_is_fingerprinted_by_fpcalc_and_looked_up(monkeypatch, tmp_path):
    _tone(tmp_path / "tone.wav")
    service = _Service(monkeypatch)
    provider = AcoustIDProvider(StaticProviderContext(settings={"api_key": "synthetic-key"}))

    identity = provider.identify(WorkspaceFile(workspace=str(tmp_path), name="tone.wav"))

    assert (identity.title, identity.artist, identity.recording_id, identity.score) == ("Tone", "Sine", "rid-1", 0.93)
    (url, sent), = service.posts
    assert url == "https://api.acoustid.org/v2/lookup"
    assert sent["client"] == "synthetic-key" and len(sent["fingerprint"]) > 50 and int(float(sent["duration"])) in (7, 8)


def test_the_identity_travels_through_the_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    monkeypatch.setattr(acoustid_module, "_acoustid", _FakeMatchLib([(0.8, "rid", "Song", "Artist")]))

    identity = via_runner(
        AcoustIDProvider, "identify", WorkspaceFile(workspace=str(tmp_path), name="a.mp3"),
        settings={"api_key": "key"}, provider_name="acoustid",
    )

    assert (identity.title, identity.artist, identity.recording_id, identity.score) == ("Song", "Artist", "rid", 0.8)


def test_the_image_needs_fpcalc_and_the_library_it_is_driven_with():
    provider_dir = Path(__file__).resolve().parent.parent

    assert "libchromaprint-tools" in (provider_dir / "Dockerfile").read_text()
    assert "libchromaprint-tools" in (provider_dir / "tests" / "apt-packages.txt").read_text()
    assert "pyacoustid==1.3.0" in (provider_dir / "pyproject.toml").read_text()


def test_the_http_timeout_of_the_context_goes_to_every_request_of_the_library(monkeypatch, tmp_path):
    """pyacoustid waits forever unless it is given a timeout: the provider passes the context's."""
    monkeypatch.setattr(acoustid_module, "_ACOUSTID_LIB", True)
    lookup = _FakeAcoustidLib(response={"status": "ok"})
    monkeypatch.setattr(acoustid_module, "_acoustid", lookup)
    context = StaticProviderContext(settings={"api_key": "k"}, http_timeout=7)
    AcoustIDProvider(context).test_connection(session=object())
    match = _FakeMatchLib([(0.8, "rid", "Song", "Artist")])
    monkeypatch.setattr(acoustid_module, "_acoustid", match)
    AcoustIDProvider(context).identify(WorkspaceFile(workspace=str(tmp_path), name="a.mp3"))

    assert lookup.timeouts == [7] and match.timeout == 7


def test_the_library_is_pointed_at_https_when_the_provider_is_imported():
    import acoustid

    assert acoustid.API_BASE_URL == "https://api.acoustid.org/v2/"
