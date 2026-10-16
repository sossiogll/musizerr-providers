# The AcoustID provider image. scripts/build.sh builds it; scripts/build-provider-image.sh of the main Musizerr
# repository puts the org.musizerr.provider.* labels on it, taken from the provider itself.
FROM ghcr.io/sossiogll/musizerr-provider-base:5

# Standard OCI labels: the development catalog entry is written from them. The provider label is what the provider
# calls itself (a test checks that it is).
LABEL org.opencontainers.image.description="Identifies what an audio file really is from its fingerprint (Chromaprint + AcoustID). Needs an AcoustID API key." \
      org.opencontainers.image.url="https://acoustid.org" \
      org.opencontainers.image.source="https://github.com/sossiogll/musizerr-providers" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.musizerr.provider.label="AcoustID"

# `fpcalc`, the Chromaprint fingerprinter pyacoustid runs on the file. It is a program, not a Python package, and it
# runs inside the sandbox next to the provider: the image carries it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/* \
    && fpcalc -version

COPY . /tmp/provider
RUN pip install /tmp/provider && rm -rf /tmp/provider

# The gateway appends --socket and --egress-socket.
ENTRYPOINT ["python", "-m", "musizerr_sdk.runner", "--provider", "musizerr_provider_acoustid:AcoustIDProvider"]
