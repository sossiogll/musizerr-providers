#!/bin/sh
# Print the providers whose current version has no image on the registry yet: the ones a push to dev publishes
# (.github/workflows/publish.yml).
#
#   scripts/unpublished.sh [provider...]
#
#   provider        folders of this repository (default: all of them)
#
#   GHCR_NAMESPACE  where the images are (default ghcr.io/sossiogll)
#
# For each provider it asks the registry for <namespace>/musizerr-provider-<folder>:<version of its pyproject.toml>
# (`docker buildx imagetools inspect`; log in to the registry first to see private packages) and prints the folder of
# each one that is not there, one per line. A provider folder is named after the provider (the name its image is
# published under). A registry that answers "not found", or refuses access (a package that does not exist yet), counts
# as not published: the push that follows would be refused too, so a published version is never overwritten. Any other
# failure (the network) is an error. When the image of the version exists but catalog.json pins another digest, it
# warns on stderr: raise the version to publish it again. Needs Docker (with buildx) and jq.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAMESPACE=${GHCR_NAMESPACE:-ghcr.io/sossiogll}

if [ $# -eq 0 ]; then
  # shellcheck disable=SC2046 # one word per folder
  set -- $(for dir in "$ROOT"/*/; do [ -f "$dir/Dockerfile" ] && [ -f "$dir/pyproject.toml" ] && basename "$dir"; done)
fi
warn() {
  if [ "${GITHUB_ACTIONS:-}" = true ]; then echo "::warning::$*" >&2; else echo "warning: $*" >&2; fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for provider in "$@"; do
  provider=${provider%/}
  [ -f "$ROOT/$provider/Dockerfile" ] || { echo "$provider is not a provider folder of this repository" >&2; exit 2; }
  version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/$provider/pyproject.toml" | head -n 1)
  [ -n "$version" ] || { echo "cannot read the version from $provider/pyproject.toml" >&2; exit 2; }
  image=$NAMESPACE/musizerr-provider-$provider
  if digest=$(docker buildx imagetools inspect "$image:$version" --format "{{.Manifest.Digest}}" 2>"$TMP/error"); then
    pinned=$(jq -r --arg image "$image" '[.providers[] | select(.image == $image) | .digest][0] // empty' "$ROOT/catalog.json" 2>/dev/null || true)
    if [ -n "$digest" ] && [ "$digest" != "$pinned" ]; then
      warn "$image:$version is published ($digest) but catalog.json pins ${pinned:-nothing} for it: raise the version of $provider to publish it again"
    fi
    echo "$provider $version: published" >&2
    continue
  fi
  if grep -qiE 'not found|manifest unknown|name unknown|denied|unauthorized|forbidden' "$TMP/error"; then
    echo "$provider $version: not published" >&2
    echo "$provider"
  else
    cat "$TMP/error" >&2
    echo "cannot tell whether $image:$version is published" >&2
    exit 1
  fi
done
