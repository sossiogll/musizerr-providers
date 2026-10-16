# The Navidrome provider image. scripts/build.sh builds it; scripts/build-provider-image.sh of the main Musizerr
# repository puts the org.musizerr.provider.* labels on it, taken from the provider itself.
FROM ghcr.io/sossiogll/musizerr-provider-base:5

# Standard OCI labels: the development catalog entry is written from them. The provider label is what the provider
# calls itself (a test checks that it is).
LABEL org.opencontainers.image.description="Browse your Navidrome library, see what you already own, and keep likes and playlists in sync with it." \
      org.opencontainers.image.url="https://www.navidrome.org" \
      org.opencontainers.image.source="https://github.com/sossiogll/musizerr-providers" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.musizerr.provider.label="Navidrome"

COPY . /tmp/provider
RUN pip install /tmp/provider && rm -rf /tmp/provider

# The gateway appends --socket and --egress-socket.
ENTRYPOINT ["python", "-m", "musizerr_sdk.runner", "--provider", "musizerr_provider_navidrome:NavidromeProvider"]
