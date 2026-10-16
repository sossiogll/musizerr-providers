#!/bin/sh
# Build the images of the providers, push them to a registry and list them in a catalog.
#
#   scripts/build.sh --registry HOST:PORT --catalog-dir DIR [--catalog NAME] [--install-registry NAME:PORT] [provider...]
#
#   --registry          the registry to push to, as THIS machine reaches it: a LOCAL registry (docker run -d -p
#                       127.0.0.1:18800:5000 registry:2). The script of the main repository it calls refuses ghcr.io and
#                       Docker Hub: nothing is ever pushed there from here.
#   --catalog-dir       the folder the catalog file is written in. Required: it is never the main checkout.
#   --catalog           the catalog's name (default: providers): DIR/NAME.json, catalog version 2 (docs/catalog.md of the
#                       main repository). Running again for the same provider moves its digest on; the others stay.
#   --install-registry  how the gateway reaches the registry, the name the entries give the images (default
#                       oci-registry:5000, the compose service of the main repository's development setup).
#   provider            folders of this repository (default: all of them).
#
#   MUSIZERR_REPO       a checkout of the main Musizerr repository (default: ../musizerr, next to this one). Its
#                       scripts/dev-catalog.sh does the work (and scripts/build-provider-image.sh under it): it builds
#                       the base image ghcr.io/sossiogll/musizerr-provider-base:5 from its provider-base/ and sdk/ (a
#                       local name, never pushed), builds each provider here on it, asks the provider for its
#                       org.musizerr.provider.* labels, builds again with them, pushes by tag and writes the entry
#                       with the digest. The main repository is only read.
#   PROVIDER_LOCAL_TAG_PREFIX  prefix of the local image names (default musizerr-provider-), to keep a test run apart
#                       from images you use.
#
# Needs Docker. CI, later, will do the same with the real registry and tags.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO_PATH=${MUSIZERR_REPO:-$ROOT/../musizerr}
REGISTRY=""
CATALOG_DIR=""
CATALOG=providers
INSTALL_REGISTRY=oci-registry:5000

die() { echo "$*" >&2; exit 2; }
usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
while [ $# -gt 0 ]; do
  case $1 in
    --registry) [ $# -ge 2 ] || usage; REGISTRY=$2; shift 2 ;;
    --catalog-dir) [ $# -ge 2 ] || usage; CATALOG_DIR=$2; shift 2 ;;
    --catalog) [ $# -ge 2 ] || usage; CATALOG=$2; shift 2 ;;
    --install-registry) [ $# -ge 2 ] || usage; INSTALL_REGISTRY=$2; shift 2 ;;
    -h|--help) usage ;;
    -*) die "unknown option $1" ;;
    *) break ;;
  esac
done
[ -n "$REGISTRY" ] || die "--registry is required (a local registry, HOST:PORT)"
[ -n "$CATALOG_DIR" ] || die "--catalog-dir is required"
[ -x "$REPO_PATH/scripts/dev-catalog.sh" ] || die "MUSIZERR_REPO ($REPO_PATH) is not a checkout of the Musizerr repository"
case "$REGISTRY" in
  ghcr.io*|docker.io*|index.docker.io*) die "refusing to push to $REGISTRY from this script" ;;
esac
mkdir -p "$CATALOG_DIR"
CATALOG_DIR=$(cd "$CATALOG_DIR" && pwd)

if [ $# -eq 0 ]; then
  set -- $(for dir in "$ROOT"/*/; do [ -f "$dir/Dockerfile" ] && basename "$dir"; done)
fi
for provider in "$@"; do
  [ -f "$ROOT/$provider/Dockerfile" ] || die "$provider is not a provider folder of this repository"
done
for provider in "$@"; do
  shift
  set -- "$@" "$ROOT/$provider"
done

DEV_CATALOG_DIR=$CATALOG_DIR PROVIDER_INSTALL_REGISTRY=$INSTALL_REGISTRY \
  "$REPO_PATH/scripts/dev-catalog.sh" -c "$CATALOG" -r "$REGISTRY" "$@"
