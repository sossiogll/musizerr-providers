"""
Identificazione audio via AcoustID (Chromaprint fingerprint + database AcoustID).

Serve a sapere *cosa* è davvero un file dall'audio, non dai tag (che possono
essere sbagliati). Usi: anti-mislabel post-download e dedup affidabile.

OPT-IN e "inerte" di default: senza una API key (e senza le dipendenze
``pyacoustid`` + ``fpcalc``) il provider è registrato ma ``is_available()`` è
False → i chiamanti lo saltano con grazia. Si attiva inserendo la key dalla UI.

Caveat rispettato: dal fingerprint si usano SOLO titolo e artista (più il
recording id). Album/anno NON sono affidabili e non vengono restituiti.
"""

from __future__ import annotations

import logging

from musizerr_sdk.auth import AccountModel, AuthDefinition, AuthFlow, AuthSession
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, Identity, IdentificationProvider, WorkspaceFile
from musizerr_sdk.permissions import Permissions
from musizerr_sdk.settings import ProviderSetting

logger = logging.getLogger(__name__)

_API_BASE_URL = "https://api.acoustid.org/v2/"

# Soft import: il provider resta registrabile anche senza la libreria.
try:
    import acoustid as _acoustid  # pyacoustid
    # The library's own default address is plain HTTP, and the API key travels in the request:
    # the provider asks for HTTPS instead, with the library's own setting, once, here.
    _acoustid.set_base_url(_API_BASE_URL)
    _ACOUSTID_LIB = True
except Exception:  # noqa: BLE001 — qualsiasi errore d'import ⇒ "non installato"
    _acoustid = None
    _ACOUSTID_LIB = False


class AcoustIDProvider(IdentificationProvider):
    name = "acoustid"
    label = "AcoustID"
    provider_api_version = PROVIDER_API_VERSION
    # The only place it talks to: the AcoustID web service over HTTPS (_API_BASE_URL, set on pyacoustid at import).
    # `fpcalc` (Chromaprint) fingerprints the file locally and needs no network.
    permissions = Permissions(egress=("api.acoustid.org:443",))
    # No account: the key is a setting of the instance, not a login.
    auth = AuthDefinition(flow=AuthFlow.NONE, account_model=AccountModel.NONE)

    def config_schema(self) -> list[ProviderSetting]:
        # Config SPECIFICA del provider (la chiave del servizio): vive qui,
        # non nell'host. I toggle/soglia GENERICI (identification_enabled,
        # identification_confidence_threshold) restano invece nel catalogo
        # dell'host, provider-agnostici.
        return [
            ProviderSetting(
                key="api_key", type="string", default=None,
                label="AcoustID API key", required=True, secret=True,
                description="Chiave API AcoustID. Senza chiave l'identificazione resta disattivata."),
            # Un rate (richieste/secondo): float, non testo.
            ProviderSetting(
                key="rate_limit_per_sec", type="float", default=None,
                label="Rate limit (req/s)", required=False,
                description="Richieste al secondo verso l'API AcoustID."),
        ]

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("api_key"))

    def test_connection(self, session: AuthSession) -> str | None:
        if not _ACOUSTID_LIB:
            return "libreria pyacoustid non installata"
        api_key = self._api_key()
        if not api_key:
            return "API key AcoustID non configurata"

        # Validare SOLO la chiave non richiede un fingerprint reale (quindi
        # niente fpcalc/chromaprint qui): il servizio AcoustID distingue con
        # un error code una chiave non valida (4, "invalid API key") da un
        # fingerprint non valido (3, "invalid fingerprint"). Una richiesta
        # deliberatamente minima e fittizia cade sempre nel secondo caso se
        # e solo se la chiave è valida — è il segnale che ci serve.
        try:
            response = _acoustid.lookup(api_key, "-", 1, meta=[], timeout=self.context.http_timeout)
        except Exception as exc:  # noqa: BLE001 — rete/DNS/timeout verso il servizio
            return f"Impossibile contattare AcoustID: {exc}"

        if response.get("status") == "ok":
            return None

        error = response.get("error") or {}
        code = error.get("code")
        if code == 4:
            return "API key AcoustID non valida"
        if code == 3:
            # "invalid fingerprint" per una richiesta che non ne aveva uno
            # vero: la chiave stessa è stata accettata.
            return None
        return f"AcoustID: {error.get('message') or 'risposta inattesa dal servizio'}"

    # ------------------------------------------------------------------

    def _get_config(self) -> dict:
        """Config SPECIFICA di questo provider (stessa via degli altri provider)."""
        return dict(self.context.settings())

    def _api_key(self) -> str:
        return str(self._get_config().get("api_key") or "")

    def is_available(self) -> bool:
        """Il provider PUÒ lavorare: libreria presente e configurato (API key
        impostata). Deliberatamente NON consulta il toggle GLOBALE
        `identification_enabled` — quella è la decisione "il sistema deve
        fare identificazione?", separata da "questo provider specifico ci
        riesce?", e vive nei call site generici (es.
        app/workers/tasks.py::_check_mislabel), mai qui: se la disponibilità
        dipendesse dal toggle, accendere `identification_enabled` (che a sua
        volta richiede un provider usable) sarebbe impossibile finché il
        toggle non è già acceso."""
        return bool(_ACOUSTID_LIB and self._api_key())

    def identify(self, audio: WorkspaceFile) -> Identity | None:
        if not self.is_available():
            return None

        api_key = self._api_key()
        try:
            best = None  # (score, recording_id, title, artist)
            for score, rid, title, artist in _acoustid.match(api_key, audio.path, timeout=self.context.http_timeout):
                if best is None or (score or 0) > (best[0] or 0):
                    best = (score, rid, title, artist)
        except Exception as exc:  # noqa: BLE001 — rete/rate-limit/fingerprint
            logger.warning("AcoustID lookup failed for %s: %s", audio.name, exc)
            return None

        if best is None:
            return None
        score, rid, title, artist = best
        if not title or not artist:
            # Dato non valido (manca titolo o artista): non è una decisione
            # di policy, è un fingerprint che non porta informazione
            # sufficiente ad affermare un'identità.
            return None
        return Identity(title=title, artist=artist, score=float(score or 0), recording_id=rid)
