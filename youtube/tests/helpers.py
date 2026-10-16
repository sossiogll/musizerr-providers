"""Small helpers of these tests: a track to build, the capabilities a provider declares, the host's page loop,
and a call made through the runner."""
from __future__ import annotations

from typing import Any

from musizerr_sdk import protocol
from musizerr_sdk.capabilities import CAPABILITY_DISCOGRAPHY, CAPABILITY_UNLIKE_SAVED_TRACK, CAPABILITY_USER_LIBRARY
from musizerr_sdk.context import StaticProviderContext
from musizerr_sdk.identifiers import Identifier
from musizerr_sdk.models import Track
from musizerr_sdk.runner import ProviderRunner


def make_track(
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
    *,
    duration_ms: int = 200_000,
    provider: str = "other",
    provider_id: str | None = None,
    isrc: str | None = None,
) -> Track:
    ids = [Identifier(type="isrc", value=isrc)] if isrc else []
    return Track(
        title=title, artist=artist, album=album, duration_ms=duration_ms, provider=provider,
        provider_id=provider_id or f"{provider}-{title}", external_ids=ids,
    )


def declared_capabilities(provider) -> list[str]:
    """The capability keys a metadata provider declares with its ``supports_*`` methods, in the order the host lists them."""
    declared = (
        (CAPABILITY_USER_LIBRARY, provider.supports_user_library),
        (CAPABILITY_UNLIKE_SAVED_TRACK, provider.supports_unlike_saved_track),
        (CAPABILITY_DISCOGRAPHY, provider.supports_discography),
    )
    return [key for key, supports in declared if supports()]


def fetch_all_saved_tracks(provider, session, page_size=50, on_page=None):
    """How the host imports the likes of a provider (the same loop as its library sync): every page
    ``iter_saved_tracks`` gives, the provider deciding where its library ends."""
    all_tracks = []
    for batch in provider.iter_saved_tracks(session, page_size):
        all_tracks.extend(batch)
        if on_page is not None:
            on_page(len(all_tracks))
    return all_tracks


class RemoteFailure(Exception):
    """A failure the runner classified (not one of the contract's own exceptions)."""

    def __init__(self, remote) -> None:
        super().__init__(remote.message)
        self.kind = remote.kind
        self.type_name = remote.type_name
        self.retry_after = remote.retry_after


def via_runner(factory, method: str, *args: Any, settings: dict | None = None, provider_name: str = "provider", **kwargs: Any):
    """``method`` called the way the host calls it in a sandbox: encoded, handled by a runner, decoded."""
    context = StaticProviderContext(settings=settings, provider_name=provider_name)
    request = protocol.call_request(method, args, kwargs, context.snapshot())
    return protocol.read_reply(ProviderRunner(factory).handle(request), RemoteFailure)
