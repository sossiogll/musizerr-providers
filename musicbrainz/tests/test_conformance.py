"""The rules every provider follows, checked by the SDK on this one (musizerr_sdk.conformance)."""
from musizerr_sdk.conformance import check_provider
from musizerr_sdk.context import StaticProviderContext

from musizerr_provider_musicbrainz import MusicBrainzMetadataProvider


def test_the_provider_follows_the_generic_rules_of_the_contract():
    check_provider(MusicBrainzMetadataProvider(StaticProviderContext()))
