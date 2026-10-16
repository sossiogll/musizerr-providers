"""
YouTube likes importer.

This provider has NO catalog: it never searches, never lists albums or
artists, never plays or downloads anything (YouTube's terms forbid separating
the audio). It only reads the videos the user liked and keeps the ones of the
Music category; the like matching then links them to the library (there is no
ISRC on YouTube: it matches by title, artist and duration). With the user's
setting ``unlike_on_origin_providers`` on, taking a favorite off also takes the
like off the video.

The Data API v3 quota is 10,000 units a day for the whole Google Cloud project
(shared by every user of the instance): ``videos.list`` costs 1 unit a page of
50, ``videos.rate`` 50, ``search.list`` 100 and is NEVER called here.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterator

import httpx

from musizerr_sdk.auth import (
    AccountModel,
    AuthDefinition,
    AuthFlow,
    AuthSession,
    BearerHeaderRequestAuth,
    OAuth2Config,
    RequestBodyClientAuth,
)
from musizerr_sdk.capabilities import CAPABILITY_UNLIKE_SAVED_TRACK
from musizerr_sdk.errors import ProviderRateLimited
from musizerr_sdk.identifiers import PROVIDER_ID, Identifier
from musizerr_sdk.interfaces import PROVIDER_API_VERSION, MetadataProvider
from musizerr_sdk.models import Track
from musizerr_sdk.permissions import Permissions
from musizerr_sdk.settings import ProviderSetting
from musizerr_sdk.text import normalize, strip_video_noise

logger = logging.getLogger(__name__)

_BASE = "https://www.googleapis.com/youtube/v3"
_PAGE_MAX = 50  # the most videos.list returns in one call (1 quota unit)
_MUSIC_CATEGORY_ID = "10"
_PARTS = "snippet,contentDetails"

# The reasons (``error.errors[].reason`` / ``error.details[].reason``) of a
# 403/429 that mean "not now", not "not allowed".
_QUOTA_REASONS = frozenset({
    "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
    "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED",
})
# ... and of a 403 that means "the connection lacks the scope this call needs".
_SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})

_TOPIC_SUFFIX_RE = re.compile(r"\s+-\s+Topic\s*$", re.I)
_VEVO_SUFFIX_RE = re.compile(r"\s*vevo\s*$", re.I)
_TITLE_SEPARATOR_RE = re.compile(r"\s[-–—]\s")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)
_MAX_CREDIT_WORDS = 8


class YouTubeMetadataProvider(MetadataProvider):
    name = "youtube"
    label = "YouTube"
    provider_api_version = PROVIDER_API_VERSION
    # The Data API is the only place the provider itself talks to. The Google sign-in pages (authorize_url and
    # token_url below) are visited by the host, which runs the OAuth flow, never by the provider.
    permissions = Permissions(egress=("www.googleapis.com:443",))
    # Only the likes of each user are on offer (no app-only access to them), so
    # there is no shared account to fall back on.
    auth = AuthDefinition(
        flow=AuthFlow.OAUTH2,
        account_model=AccountModel.PERSONAL,
        oauth2=OAuth2Config(
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/youtube.readonly"],
            token_client_auth=RequestBodyClientAuth(),
            request_auth=BearerHeaderRequestAuth(),
            # No fallback: the likes are the user's own, there is no app-only
            # access to them.
            fallback_auth=None,
            # Taking a like off needs the full YouTube scope. A connection made
            # without it is told to reconnect.
            capability_scopes={CAPABILITY_UNLIKE_SAVED_TRACK: ["https://www.googleapis.com/auth/youtube"]},
            # Google hands out a refresh token only to a request that asks for
            # offline access, and again only when the consent is shown.
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        ),
    )

    def config_schema(self) -> list[ProviderSetting]:
        # The OAuth client (id and secret) is saved from the "App" section of
        # the card, never declared here. Nothing else is configurable: there
        # is no catalog to tune.
        return []

    def validate_config(self, config: dict) -> bool:
        return True

    def test_connection(self, session: AuthSession) -> str | None:
        """One ``videos.list`` of a single like (1 quota unit)."""
        try:
            self._get("videos", {"part": "id", "myRating": "like", "maxResults": 1}, session)
            return None
        except (httpx.HTTPError, ProviderRateLimited, PermissionError) as e:
            return str(e)

    # ------------------------------------------------------------------
    # By id (the one lookup the host can ask for a liked video)
    # ------------------------------------------------------------------

    def get_track(self, provider_id: str, session: AuthSession) -> Track | None:
        """One ``videos.list`` by id (1 quota unit); no category filter: the
        video was asked for by name."""
        data = self._get("videos", {"part": _PARTS, "id": provider_id}, session)
        items = data.get("items") or []
        return self._video_to_track(items[0], music_only=False) if items else None

    # ------------------------------------------------------------------
    # User library
    # ------------------------------------------------------------------

    def supports_user_library(self) -> bool:
        return True

    def iter_saved_tracks(self, session: AuthSession, page_size: int = _PAGE_MAX) -> Iterator[list[Track]]:
        """The liked videos of the Music category, one list per page of the
        API: ONE ``videos.list`` call (1 quota unit) per page, following
        ``nextPageToken``. The end of the library is the absence of a next
        page token, never the length of the kept list: dropping the videos of
        other categories leaves a short (even empty) page with more pages
        behind it. The cursor lives in this generator, so concurrent syncs of
        different users never share it."""
        params: dict[str, Any] = {
            "part": _PARTS, "myRating": "like", "maxResults": max(1, min(page_size, _PAGE_MAX)),
        }
        seen: set[str] = set()
        while True:
            data = self._get("videos", params, session)
            yield [t for t in (self._video_to_track(v) for v in data.get("items") or []) if t is not None]
            token = data.get("nextPageToken")
            if not token or token in seen:
                return
            seen.add(token)
            params = {**params, "pageToken": token}

    def get_saved_tracks(self, session: AuthSession, limit: int = 50, offset: int = 0) -> list[Track]:
        """``limit`` liked music videos from position ``offset``. Walks the
        pages from the first one (YouTube has no offset): one quota unit per
        page read, so it is for a bounded look, not for paging a whole
        library by offset: the host imports through ``iter_saved_tracks``."""
        kept: list[Track] = []
        for page in self.iter_saved_tracks(session):
            kept.extend(page)
            if len(kept) >= offset + limit:
                break
        return kept[offset:offset + limit]

    def supports_unlike_saved_track(self) -> bool:
        return True

    def unlike_saved_track(self, provider_id: str, session: AuthSession) -> None:
        """``videos.rate`` with rating ``none`` (50 quota units)."""
        resp = httpx.post(
            f"{_BASE}/videos/rate",
            params={"id": provider_id, "rating": "none"},
            headers=session.auth_headers(),
            timeout=self.context.http_timeout,
        )
        self._check(resp, scope_hint="manage likes")

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any], session: AuthSession) -> dict[str, Any]:
        resp = httpx.get(
            f"{_BASE}/{path}",
            params=params,
            headers=session.auth_headers(),
            timeout=self.context.http_timeout,
        )
        self._check(resp, scope_hint="read likes")
        return resp.json()

    @staticmethod
    def _check(resp: httpx.Response, *, scope_hint: str) -> None:
        """Passes a success; turns a refused call into what the host
        understands: an exhausted quota is ``ProviderRateLimited`` (not a
        refused credential, not "no likes"), a connection without the scope is
        ``PermissionError`` (the user reconnects), anything else the HTTP
        error itself."""
        if resp.status_code < 400:
            return
        reasons, message = _error_reasons(resp)
        logger.warning("YouTube answered %d (%s): %s", resp.status_code, ", ".join(sorted(reasons)) or "-", message)
        if resp.status_code == 429 or reasons & _QUOTA_REASONS:
            raise ProviderRateLimited(f"YouTube quota or rate limit reached ({', '.join(sorted(reasons)) or 429})")
        if resp.status_code == 401 or (resp.status_code == 403 and reasons & _SCOPE_REASONS):
            raise PermissionError(
                f"YouTube access not authorized: reconnect YouTube to allow the app to {scope_hint} "
                "(Settings → Linked accounts → YouTube)"
            )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _video_to_track(self, video: dict[str, Any], *, music_only: bool = True) -> Track | None:
        snippet = video.get("snippet") or {}
        video_id = video.get("id")
        if not video_id or (music_only and snippet.get("categoryId") != _MUSIC_CATEGORY_ID):
            return None
        artist, title = _artist_and_title(snippet.get("title") or "", snippet.get("channelTitle") or "")
        track = Track(
            title=title,
            artist=artist,
            album="",
            duration_ms=_duration_ms((video.get("contentDetails") or {}).get("duration")),
            cover_url=_best_thumbnail(snippet.get("thumbnails") or {}),
            provider=self.name,
            provider_id=video_id,
        )
        track.add_id(Identifier(type=PROVIDER_ID, value=video_id, provider=self.name))
        # No ``saved_at``: the API does not say when the video was liked (its
        # ``publishedAt`` is the upload, not the like). No preview/stream
        # URL: the audio is never separated from the video.
        return track


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def _error_reasons(resp: httpx.Response) -> tuple[set[str], str]:
    """The reasons and the message of a Google API error body."""
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        return set(), resp.text[:200]
    if not isinstance(error, dict):
        return set(), str(error)[:200]
    reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
    reasons |= {d.get("reason") for d in error.get("details") or [] if isinstance(d, dict)}
    if error.get("status"):
        reasons.add(error["status"])
    return {r for r in reasons if isinstance(r, str)}, str(error.get("message") or "")


def _duration_ms(iso: str | None) -> int:
    """Milliseconds of an ISO 8601 duration (``PT4M13S``, ``PT1H2M``,
    ``P0D`` of a live stream); 0 when unknown."""
    match = _ISO_DURATION_RE.match(iso or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (float(match.group(k) or 0) for k in "dhms")
    return int(((days * 24 + hours) * 60 + minutes) * 60_000 + seconds * 1000)


def _best_thumbnail(thumbnails: dict[str, Any]) -> str | None:
    """The largest thumbnail the video has."""
    sized = [t for t in thumbnails.values() if isinstance(t, dict) and t.get("url")]
    if not sized:
        return None
    return max(sized, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))["url"]


def _channel_artist(channel: str) -> tuple[str, bool]:
    """The artist a channel name stands for and whether the channel is the
    artist's own auto-generated one (``Name - Topic``, whose videos carry the
    plain song title). ``NameVEVO`` is ``Name``."""
    channel = channel.strip()
    if _TOPIC_SUFFIX_RE.search(channel):
        return _TOPIC_SUFFIX_RE.sub("", channel).strip(), True
    if _VEVO_SUFFIX_RE.search(channel):
        name = _VEVO_SUFFIX_RE.sub("", channel).strip()
        # "TaylorSwiftVEVO": the words were glued together.
        return (_CAMEL_RE.sub(" ", name) if " " not in name else name), False
    return channel, False


def _compact(text: str) -> str:
    return normalize(text).replace(" ", "")


def _same_name(a: str, b: str) -> bool:
    """Two spellings of one name (spaces, case, accents aside)."""
    a, b = _compact(a), _compact(b)
    return bool(a and b and (a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a))))


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) > 2 and text[0] in "\"“" and text[-1] in "\"”":
        return text[1:-1].strip()
    return text


def _artist_and_title(video_title: str, channel: str) -> tuple[str, str]:
    """``(artist, title)`` of a music video: the title without its video
    decoration, the artist from the channel (``Name - Topic``, ``NameVEVO``)
    or, on any other channel, from a title written ``Artist - Title`` when the
    left side is the channel or plausibly an artist."""
    title = _unquote(strip_video_noise(video_title.strip())) or video_title.strip()
    channel_artist, is_topic = _channel_artist(channel)
    if is_topic:
        return channel_artist, title
    parts = _TITLE_SEPARATOR_RE.split(title, maxsplit=1)
    if len(parts) == 2:
        left, right = (_unquote(p) for p in parts)
        if left and right:
            if _same_name(left, channel_artist) or (
                len(left.split()) <= _MAX_CREDIT_WORDS and not left.isdigit() and not _same_name(right, channel_artist)
            ):
                return left, right
            if _same_name(right, channel_artist):
                # "Title - Artist", with the channel naming the artist.
                return channel_artist, left
    return channel_artist, title
