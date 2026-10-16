# MusicBrainz (metadata)

Music metadata from [MusicBrainz](https://musicbrainz.org), the open music encyclopedia: search for tracks
(recordings), albums (releases) and artists, look them up by id, list an artist's discography (release groups
of the types album, single and EP) and find a recording by its ISRC. A recording that appears on several
releases becomes one track per release (the id of a track is `recording::release`, on purpose).

The universal identifiers it gives (the MusicBrainz id and the ISRC) are what lets Musizerr tell that a track of
another catalog is the same recording.

## Settings

| Key | Meaning |
| --- | --- |
| `search_limit` | Results per search (optional; MusicBrainz allows up to 100). |

There is no account and no key: MusicBrainz is public. Its terms ask clients for at most one request per second.

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- reach `musicbrainz.org` on port 443 (HTTPS). It follows no redirects, so no other host is ever contacted.

No folders, no raw TCP, no declared resource limits (the gateway's defaults apply).

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh musicbrainz
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs musicbrainz
```
