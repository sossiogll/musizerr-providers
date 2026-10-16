# LRCLIB (lyrics)

Synced (`.lrc`, line by line) and plain lyrics from [lrclib.net](https://lrclib.net), a free and open lyrics
database. A lyrics provider: Musizerr asks it for the lyrics of a track and shows them next to the player.

The provider asks for an exact match first (artist, title, album and duration), and, when there is none,
for the best result of a search on artist and title. A failure of the service is "no lyrics", never an error
for the user.

## Settings

None. LRCLIB is public: there is no account, no key and nothing to configure.

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- reach `lrclib.net` on port 443 (HTTPS). It follows no redirects, so no other host is ever contacted.

No folders, no raw TCP, no declared resource limits (the gateway's defaults apply).

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh lrclib
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs lrclib
```
