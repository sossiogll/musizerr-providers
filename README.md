# Musizerr providers

The first-party **public providers** of [Musizerr](https://github.com/sossiogll/musizerr), the self-hosted,
provider-agnostic music manager. Every provider here is a plugin in its own right: a Python package, built into its own
[OCI image](https://opencontainers.org) on top of the Musizerr provider base image, that declares exactly what it needs
(which hosts it talks to, which folder it writes) and runs in a sandbox that grants nothing else.

> Based on Musizerr — https://github.com/sossiogll/musizerr

## The providers

| Folder | Provider | Role | What it does | What the sandbox grants it |
| --- | --- | --- | --- | --- |
| [`lrclib/`](lrclib/) | LRCLIB | lyrics | Synced and plain lyrics from lrclib.net | `lrclib.net:443` |
| [`musicbrainz/`](musicbrainz/) | MusicBrainz | metadata | Search, lookups, discography and ISRC lookup | `musicbrainz.org:443` |
| [`youtube/`](youtube/) | YouTube | metadata | Imports the videos a user liked (Music category) as favorites; no catalog, no playback | `www.googleapis.com:443` |
| [`lidarr/`](lidarr/) | Lidarr | download | Asks your Lidarr server to get an album and follows the import | `musicbrainz.org:443` and the server in the `url` setting |
| [`acoustid/`](acoustid/) | AcoustID | identification | Identifies a file from its audio fingerprint (`fpcalc` is in the image) | `api.acoustid.org:443` |
| [`ftp/`](ftp/) | FTP Storage | storage | The library on an FTP server, passive mode, streamed with seek | raw TCP, any port, to the host in the `host` setting |
| [`local/`](local/) | Local Storage | storage | The library in a folder of the machine | read and write on the folder in the `root_dir` setting |
| [`navidrome/`](navidrome/) | Navidrome | metadata, sync | Browse, scan, likes, playlists and the library listing of a Navidrome server | the server in the `url` setting |

Each folder has its own `README.md`: what the provider does, its settings and its permissions in words.

Providers that are not listed here are not part of this repository.

## What is in a provider folder

```
<name>/
  pyproject.toml               the package musizerr_provider_<name>; depends on musizerr-sdk>=5.2,<6 and what it needs
  Dockerfile                   FROM ghcr.io/sossiogll/musizerr-provider-base:5, the OCI labels, the entry point
  README.md
  musizerr_provider_<name>/    the provider (provider_api_version, permissions, auth, the contract)
  tests/                       its tests, and the tests of its permissions
```

The permissions are declared in the provider's class (`permissions = Permissions(...)`) and travel as labels of the
image (`org.musizerr.provider.*`): the admin sees them before installing, and the gateway refuses an image whose labels
differ from what was approved. Each provider's tests check that the declaration round-trips through the labels, that it
covers every address written in the code and every request the code makes, and that nothing else is allowed.

## Building and testing here

The providers are built against the SDK and the base image of the main Musizerr repository, so you need a checkout of it
next to this one (`../musizerr`), or point at it:

```sh
export MUSIZERR_REPO=/path/to/musizerr         # for build.sh and integration.sh
export MUSIZERR_SDK=$MUSIZERR_REPO/sdk         # for test.sh (default: ../musizerr/sdk)
```

Nothing runs on the host: everything runs in containers, and nothing is written to the main repository.

### Tests

```sh
scripts/test.sh                 # every provider
scripts/test.sh lrclib ftp      # some
```

Each provider's tests run in a throwaway `python:3.12-slim` container, with the SDK installed from `MUSIZERR_SDK`
(mounted read-only and copied) and the provider installed with its own dependencies, so a missing dependency in a
`pyproject.toml` shows up. A provider that needs a Debian package in its tests lists it in `tests/apt-packages.txt`
(AcoustID needs `fpcalc`). It prints the result of each provider and exits non-zero if any fails.

### Images and catalogs

```sh
docker run -d --name providers-registry -p 127.0.0.1:18800:5000 registry:2     # a LOCAL registry
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs [provider...]
```

`build.sh` calls the main repository's `scripts/dev-catalog.sh`, which builds the base image from the SDK (a local name,
`ghcr.io/sossiogll/musizerr-provider-base:5`, never pushed), builds each provider on it, asks the provider itself for its
`org.musizerr.provider.*` labels (name, roles, contract version, permissions, display label), builds again with them,
pushes to your registry and writes a catalog entry with the image's digest into `DIR/<catalog>.json` (catalog version 2,
see `docs/catalog.md` of the main repository). Running it again moves the digests on. It refuses to push to `ghcr.io`
or Docker Hub: nothing leaves your machine.

Serve the catalog folder (any static file server, for example `python -m http.server`) and add its URL as a source in
Musizerr, with *allow insecure registry* because the local registry is plain HTTP.

### Publishing

```sh
docker login ghcr.io          # a token that may write packages
scripts/publish.sh [provider...]
```

`publish.sh` calls the main repository's `scripts/publish-provider-image.sh` for each provider: it builds the provider on
the published base image, takes its labels from the provider itself, pushes
`ghcr.io/sossiogll/musizerr-provider-<name>:<version>` for `linux/amd64` and `linux/arm64` and writes the entry in
[`catalog.json`](catalog.json), pinned by the digest of the multi-architecture index. Commit `catalog.json` afterwards: it
is what a Musizerr source points at, `https://raw.githubusercontent.com/sossiogll/musizerr-providers/master/catalog.json`.

### Integration test

```sh
scripts/integration.sh
```

Builds and pushes all the images to a local registry (`127.0.0.1:18800`), writes the catalog, starts the real gateway
(built from the main repository) in its own compose project (`mzp52`), installs every provider with the host's own
command, and checks with the host's own provider registry that each one runs, is described with the permissions it
declares, and does what it can do without an account: lyrics and MusicBrainz answer real calls, local storage and FTP
storage (against a throwaway FTP server) store, stream, list and delete, AcoustID's `fpcalc` runs in the sandbox, and
Navidrome, Lidarr and YouTube fail cleanly when their server is unreachable or not configured. It tears everything down,
the images it built included. See the header of the script.

## How the images are published

Today they are built by hand, as above. Continuous integration will do it: on a change, a GitHub Actions workflow tests
every provider, builds the multi-architecture (`linux/amd64`, `linux/arm64`) image of each, pushes it to
`ghcr.io/sossiogll/musizerr-provider-<name>` and regenerates the catalog with the new digests. A catalog entry pins an
image **by digest**; an update is a new digest the admin approves, seeing any difference in permissions.

## Adding or changing a provider

- Write the provider against `musizerr_sdk` only: the standard library, the SDK and its own dependencies. It never
  touches Redis, the database, the process environment or a host path; files cross through the workspace it is given.
- Declare `provider_api_version = PROVIDER_API_VERSION` and **precisely** the permissions it needs, from what the code
  contacts: a fixed `egress` host for a service, `egress_from_settings` for an address the user types, `tcp_from_settings`
  for a protocol that is not HTTP (and `musizerr_sdk.net.route_module` for its library), `filesystem_from_settings`
  for a folder. A wildcard host (`*.example.com:443`) only where the provider follows redirects to subdomains it cannot
  list, and say why in the provider's README. Add tests for every one, as the existing providers do.
- Keep the provider's name stable: settings, credentials and identifiers are filed under it.

## License

GNU Affero General Public License, version 3 or later, with the additional terms of Musizerr: see
[`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md). The provider SDK these providers are built on
(`musizerr-sdk`) is licensed separately under the LGPL.
