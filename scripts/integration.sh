#!/bin/sh
# Integration test of the providers of this repository: their images, built and pushed to a local registry, listed in a
# catalog, installed through the host's own command and run by the real gateway in its sandbox; then the host's own
# code (the provider registry, the contexts, the grants) talks to each of them.
#
#   scripts/integration.sh
#
#   MUSIZERR_REPO   a checkout of the main Musizerr repository (default ../musizerr, next to this one). It is only READ:
#                   its gateway is built from it, its scripts build the images, and its code runs in a container of the
#                   application image (HOST_IMAGE, default musizerr-app: only its Python and dependencies are used).
#                   Nothing is written to it.
#
# Needs Docker (with user namespaces in its kernel), network access (the base images, PyPI, lrclib.net, musicbrainz.org,
# www.googleapis.com and api.acoustid.org: the providers that need no account make real calls, one or two each). It uses
# its own compose project, `mzp52` (scripts/integration/compose.yml), publishes only 127.0.0.1:18800 (a local registry),
# pushes nothing outside that registry and tears everything down at the end, the images it built included. It touches no
# other container, volume or image.
#
# It checks that:
#   a. scripts/build.sh builds the base image and the eight provider images, pushes them and writes a valid catalog;
#   b. the catalog installs each provider through the host's command, which shows its permissions in words;
#   c. (scripts/integration/host_check.py, the application's own registry against the real gateway) every provider runs
#      and is described with the roles and permissions it declares; LRCLIB and MusicBrainz answer real calls; local
#      storage stores, streams, lists and deletes in a granted folder; FTP storage does the same against a throwaway
#      FTP server in passive mode; AcoustID's fpcalc runs inside the sandbox; Navidrome, Lidarr and YouTube fail cleanly
#      when their server is unreachable or not configured, and nothing the providers did was refused by the gateway.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
REPO_PATH=${MUSIZERR_REPO:-$ROOT/../musizerr}
[ -x "$REPO_PATH/scripts/dev-catalog.sh" ] || { echo "MUSIZERR_REPO ($REPO_PATH) is not a checkout of the Musizerr repository" >&2; exit 2; }
MUSIZERR_REPO=$(cd "$REPO_PATH" && pwd)
P=mzp52
TMP=$(mktemp -d)
CATALOGS=$TMP/catalogs
mkdir -p "$CATALOGS" "$TMP/hostdata" "$TMP/work"
chmod 777 "$TMP/hostdata" "$TMP/work"

export MUSIZERR_REPO
export MZP52_CATALOG_DIR=$CATALOGS
export PROVIDER_LOCAL_TAG_PREFIX=mzp52-
export AUTH_SECRET=integration-test-secret-of-at-least-32-characters
COMPOSE="docker compose -p $P -f scripts/integration/compose.yml"
HOST_IMAGE=${HOST_IMAGE:-musizerr-app}   # the application's image: only its Python and dependencies are used
BASE=ghcr.io/sossiogll/musizerr-provider-base:5
PROVIDERS=$(for dir in */; do [ -f "$dir/Dockerfile" ] && basename "$dir"; done)
BASE_WAS_THERE=$(docker image inspect "$BASE" >/dev/null 2>&1 && echo yes || echo no)

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- gateway log ----"; $COMPOSE logs --no-color --tail 150 provider-gateway 2>&1 || true
  fi
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  for provider in $PROVIDERS; do
    docker rmi "${PROVIDER_LOCAL_TAG_PREFIX}$provider:build" >/dev/null 2>&1 || true
  done
  for provider in $PROVIDERS; do
    docker rmi "127.0.0.1:18800/musizerr-provider-$provider:dev" >/dev/null 2>&1 || true
  done
  docker rmi $P-provider-gateway >/dev/null 2>&1 || true
  [ "$BASE_WAS_THERE" = yes ] || docker rmi "$BASE" >/dev/null 2>&1 || true
  rm -rf "$TMP"
  exit $status
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
die() { echo "FAILED: $*" >&2; exit 1; }
wait_until() { # wait_until <seconds> <description> <command...>
  limit=$1; what=$2; shift 2
  i=0
  while ! "$@" >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -le $limit ] || die "timed out waiting for $what"
    sleep 1
  done
}
ctl() { $COMPOSE exec -T provider-gateway python -m musizerr_gateway.ctl "$@"; }
# The host's code, in a container of the application image, with the same sockets and library as the app has. The code
# is the main repository's, read only; the first-party providers still in its app/providers/ are removed from a COPY of
# app/ (below), which is how the registry will look once they are gone: it refuses a gateway provider named like an
# in-process one.
host() { # host <python command...>
  docker run --rm -i --network ${P}_default --user 1000:1000 \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/app:/repo/sdk/src -e DATA_DIR=/data -e AUTH_SECRET -e REDIS_URL=redis://redis:6379/0 \
    -v "$MUSIZERR_REPO":/repo:ro -v "$ROOT/scripts/integration":/checks:ro -v "$TMP/hostdata":/data -v "$TMP/work":/work \
    -v ${P}_provider_sockets:/run/musizerr -v ${P}_downloads:/downloads \
    "$HOST_IMAGE" sh -c '
      rm -rf /work/app && cp -r /repo/app /work/app
      rm -f /work/app/providers/metadata/musicbrainz.py /work/app/providers/metadata/youtube.py /work/app/providers/download/lidarr.py \
            /work/app/providers/identification/acoustid.py /work/app/providers/storage/ftp.py /work/app/providers/storage/local.py \
            /work/app/providers/sync/navidrome.py
      cd /work/app && exec "$@"
    ' sh "$@"
}
sources() { host python -m scripts.provider_sources "$@"; }
running() { ctl list | tr -d '\n ' | grep -q "\"name\":\"$1\"[^}]*\"state\":\"running\""; }
all_running() { for name in $PROVIDERS; do running "$name" || return 1; done; }

step "starting the registry, the catalog server, redis, a throwaway FTP server and the gateway (compose project $P)"
printf '{"name": "Musizerr providers (integration)", "version": 2, "providers": []}' > "$CATALOGS/registry.json"
$COMPOSE up -d --build oci-registry registry redis ftp-server provider-gateway
wait_until 90 "the gateway's control socket" ctl health
ctl list | grep -q '"providers": \[\]' || die "the gateway runs something before the host told it anything"
echo "the gateway is up and runs nothing"

step "a. scripts/build.sh builds the base image and the provider images, pushes them to the local registry and writes the catalog"
MUSIZERR_REPO=$MUSIZERR_REPO scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir "$CATALOGS" --catalog registry $PROVIDERS 2>&1 \
  | grep -E '^(added|updated|catalog:)'
for name in $PROVIDERS; do
  grep -q "\"name\": \"$name\"" "$CATALOGS/registry.json" || die "the catalog has no entry for $name"
  digest=$(curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
    "http://127.0.0.1:18800/v2/musizerr-provider-$name/manifests/dev" | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p')
  grep -q "\"digest\": \"$digest\"" "$CATALOGS/registry.json" || die "the digest of $name in the catalog is not the registry's ($digest)"
  printf '   %-12s %s\n' "$name" "$digest"
done
[ "$(grep -c '"image": "oci-registry:5000/musizerr-provider-' "$CATALOGS/registry.json")" = "$(echo $PROVIDERS | wc -w | tr -d ' ')" ] || die "the catalog does not list every provider once"

step "b. the catalog installs each provider through the host's command, with its permissions in words"
sources add-source http://registry:9000/registry.json --insecure
for name in $PROVIDERS; do
  sources install "Musizerr providers (integration)" "$name" --dry-run > "$TMP/dry-$name.txt"
  echo "--- $name"; grep -E "network:|files:|limits:|roles|permissions" "$TMP/dry-$name.txt" | sed 's/^/    /'
done
grep -q "network: lrclib.net:443" "$TMP/dry-lrclib.txt" || die "lrclib's permission was not shown"
grep -q "network: musicbrainz.org:443" "$TMP/dry-musicbrainz.txt" || die "musicbrainz' permission was not shown"
grep -q "network: www.googleapis.com:443" "$TMP/dry-youtube.txt" || die "youtube's permission was not shown"
grep -q "network: api.acoustid.org:443" "$TMP/dry-acoustid.txt" || die "acoustid's permission was not shown"
grep -q "network: musicbrainz.org:443" "$TMP/dry-lidarr.txt" && grep -q "the setting 'url' names" "$TMP/dry-lidarr.txt" || die "lidarr's permissions were not shown"
grep -q "the setting 'url' names" "$TMP/dry-navidrome.txt" || die "navidrome's permission was not shown"
grep -q "any port of the host the setting 'host' names" "$TMP/dry-ftp.txt" || die "ftp's raw TCP permission was not shown"
grep -q "the folder the setting 'root_dir' names (read and write)" "$TMP/dry-local.txt" || die "local's folder permission was not shown"
for name in $PROVIDERS; do sources install "Musizerr providers (integration)" "$name" --yes >/dev/null; done
wait_until 240 "every provider to run" all_running
ctl list | tr -d '\n ' | grep -o '"name":"[a-z]*","state":"[a-z]*"' | sed 's/^/   /'

step "c. the host's registry against the real gateway and the real images"
host python /checks/host_check.py | tee "$TMP/check.txt"
for marker in DESCRIBE_OK LYRICS_OK MUSICBRAINZ_OK LOCAL_OK FTP_OK ACOUSTID_OK YOUTUBE_OK NAVIDROME_REDACTED_OK NAVIDROME_OK LIDARR_OK GATEWAY_OK; do
  grep -q "^$marker" "$TMP/check.txt" || die "$marker is missing"
done

step "the gateway's log: fpcalc ran in the sandbox, and nothing was refused"
$COMPOSE logs --no-color provider-gateway > "$TMP/gateway.log" 2>&1
grep -E "\[acoustid\]" "$TMP/gateway.log" | sed 's/^[^|]*| /   /' | head -5
grep -q "\[acoustid\].*AcoustID lookup failed for tone.wav" "$TMP/gateway.log" || die "the log has no AcoustID lookup of the generated tone"
grep "\[acoustid\].*AcoustID lookup failed" "$TMP/gateway.log" | grep -qi "fpcalc" && die "fpcalc did not run in the sandbox"
if grep -q "egress refused" "$TMP/gateway.log"; then grep "egress refused" "$TMP/gateway.log" | head; die "the gateway refused a connection of a provider"; fi
echo "no connection was refused"
# Port 443, not 80: a lookup over plain HTTP would have been refused above. And no Subsonic token or user in the log.
grep -q "egress.*api.acoustid.org" "$TMP/gateway.log" && grep "egress.*api.acoustid.org" "$TMP/gateway.log" | head -3
if grep -E "[?&](t|s|p)=[0-9a-f]{6,}" "$TMP/gateway.log" | grep -v "=redacted" | head -3 | grep -q .; then die "a Subsonic token is in the gateway log"; fi
if grep -q "synthetic-password\|u=alice" "$TMP/gateway.log"; then die "a Subsonic credential is in the gateway log"; fi
echo "no Subsonic credential in the gateway log"

step "all checks passed"
