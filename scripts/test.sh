#!/bin/sh
# Run the tests of the providers, each in a throwaway container (nothing runs on the host).
#
#   scripts/test.sh [provider...]
#
#   provider   a folder of this repository (lrclib, musicbrainz...). Default: all of them.
#
# The provider SDK is installed from a path, not from PyPI:
#
#   MUSIZERR_SDK   the `sdk/` folder of a checkout of the main Musizerr repository.
#                  Default: ../musizerr/sdk, relative to this repository (the two checkouts side by side).
#                  It is mounted read-only and copied inside the container, so nothing is written to it.
#                  With no checkout (CI), the tests run in the base image of the providers, which already holds
#                  the SDK it was published with (ghcr.io/sossiogll/musizerr-provider-base:5).
#   TEST_IMAGE     the image the tests run in (default python:3.12-slim, or the base image with no checkout).
#                  The providers need Python 3.12.
#
# Each provider is installed with its own dependencies (`pip install ".[test]"`: the network is needed for
# PyPI) in a fresh container, so a missing dependency in a pyproject.toml shows up here. A provider whose tests need
# a Debian package (AcoustID: fpcalc) lists it in tests/apt-packages.txt. The exit status is
# non-zero if the tests of any provider fail.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
die() { echo "$*" >&2; exit 2; }
if [ -n "${MUSIZERR_SDK:-}" ] || [ -d "$ROOT/../musizerr/sdk/src/musizerr_sdk" ]; then
  SDK_PATH=${MUSIZERR_SDK:-$ROOT/../musizerr/sdk}
  [ -d "$SDK_PATH/src/musizerr_sdk" ] || die "MUSIZERR_SDK ($SDK_PATH) is not the sdk/ folder of the Musizerr repository"
  SDK_MOUNT="-v $(cd "$SDK_PATH" && pwd):/mnt/sdk:ro"
  IMAGE=${TEST_IMAGE:-python:3.12-slim}
else
  # No checkout of Musizerr: the base image of the providers holds the SDK.
  SDK_MOUNT=""
  IMAGE=${TEST_IMAGE:-ghcr.io/sossiogll/musizerr-provider-base:5}
fi

if [ $# -eq 0 ]; then
  set -- $(for dir in "$ROOT"/*/; do [ -f "$dir/pyproject.toml" ] && basename "$dir"; done)
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
failed=0
for provider in "$@"; do
  [ -f "$ROOT/$provider/pyproject.toml" ] || die "$provider is not a provider folder of this repository"
  echo "== $provider" >&2
  # The container never writes to the mounts: the SDK and the provider are copied to its own filesystem.
  docker run --rm -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_NO_CACHE_DIR=1 -e PIP_ROOT_USER_ACTION=ignore -e PYTHONDONTWRITEBYTECODE=1 \
      $SDK_MOUNT -v "$ROOT/$provider":/mnt/provider:ro "$IMAGE" sh -ec '
        sdk=""
        if [ -d /mnt/sdk ]; then cp -r /mnt/sdk /tmp/sdk; sdk=/tmp/sdk; fi
        cp -r /mnt/provider /tmp/provider
        if [ -f /tmp/provider/tests/apt-packages.txt ]; then
          apt-get update -qq >/dev/null
          DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $(cat /tmp/provider/tests/apt-packages.txt) >/dev/null
        fi
        pip install -q $sdk "/tmp/provider[test]" >/dev/null
        cd /tmp/provider
        python -m pytest -p no:cacheprovider
      ' >"$TMP/$provider.log" 2>&1 || failed=1
  cat "$TMP/$provider.log"
done

echo >&2
echo "== summary" >&2
for provider in "$@"; do
  printf '%-12s %s\n' "$provider" "$(tail -n 1 "$TMP/$provider.log")" >&2
done
exit $failed
