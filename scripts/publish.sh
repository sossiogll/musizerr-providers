#!/bin/sh
# Publish the providers' images to GHCR (linux/amd64 and linux/arm64) and pin them in catalog.json.
#
#   scripts/publish.sh [provider...]
#
#   provider        folders of this repository (default: all of them)
#
#   MUSIZERR_REPO   a checkout of the main Musizerr repository (default: ../musizerr, next to this one). Its
#                   scripts/publish-provider-image.sh does the work: it builds each provider on the PUBLISHED base
#                   image (scripts/publish-provider-base.sh there), asks the provider for its org.musizerr.provider.*
#                   labels, builds it with them for both platforms, pushes ghcr.io/sossiogll/musizerr-provider-<name>:<version>
#                   (the version of its pyproject.toml) and writes its entry in catalog.json, pinned by the digest of the
#                   multi-architecture index. The main repository is only read.
#   GHCR_NAMESPACE  where to publish (default ghcr.io/sossiogll)
#
# Needs Docker and `docker login ghcr.io` with a token that may write packages. A new package is private and stays
# so: each Dockerfile sets org.opencontainers.image.source to this repository. Commit catalog.json afterwards: it is
# what a Musizerr source points at (docs/catalog.md of the main repository).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO_PATH=${MUSIZERR_REPO:-$ROOT/../musizerr}
[ -x "$REPO_PATH/scripts/publish-provider-image.sh" ] || { echo "MUSIZERR_REPO ($REPO_PATH) is not a checkout of the Musizerr repository" >&2; exit 2; }

if [ $# -eq 0 ]; then
  set -- $(for dir in "$ROOT"/*/; do [ -f "$dir/Dockerfile" ] && basename "$dir"; done)
fi
for provider in "$@"; do
  [ -f "$ROOT/$provider/Dockerfile" ] || { echo "$provider is not a provider folder of this repository" >&2; exit 2; }
done
for provider in "$@"; do
  echo "== $provider" >&2
  "$REPO_PATH/scripts/publish-provider-image.sh" "$ROOT/$provider" "$ROOT/catalog.json" "Musizerr providers" >/dev/null
done
echo "catalog: $ROOT/catalog.json" >&2
