# Navidrome (sync and metadata)

Connects Musizerr to your [Navidrome](https://www.navidrome.org) server through its Subsonic API. It fills two
roles with one connection:

- **Sync**: ask Navidrome to rescan after a download, list the whole library (`search3` with an empty query,
  500 songs a page) so Musizerr knows which tracks you already own and in what quality, find the candidates for a
  track, give the stream of a track you own, take part of a file for the fingerprint check, keep your **likes**
  (starred songs) in step, and mirror your **playlists** on your own account.
- **Metadata**: browse the library like a catalog (search, albums, artists, an artist's albums), and import your
  starred songs as likes.

Sign-in with a Navidrome account is supported: the server checks the credentials (`getUser`).

## Settings

| Key | Meaning |
| --- | --- |
| `url` | The address of the server, for example `http://navidrome:4533`. Required. |
| `mode` | `meta` (browse the library only) or `full` (the source of truth for what is downloaded, plus rescans). Required. |

Accounts: the shared account indexes the library; each user's own account carries their likes and playlists.

## Credentials in errors and logs

A Subsonic request carries the user, a salted token, the salt (and, for old servers, the password) in its query string,
and httpx puts that URL in its error messages and its own log line. The provider redacts `u`, `t`, `s`, `p` and
`username` (and any `Authorization` value) in the text of the errors it raises and in httpx's log line. An error keeps
its class, its status and its response, so the host classifies it as before; only its text and the URLs it holds change.
(The stream and cover addresses the provider hands to Musizerr do carry the token, because the server needs it to serve
them; they are values, not messages.)

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- reach the server the setting `url` names, on the port of that URL (its scheme's default port when it names
  none). The destination is allowed while the setting holds it, private and Tailscale addresses included, because
  you typed it. Every request the provider makes goes there: the Subsonic API and the download of a file for the
  fingerprint check. The stream and cover addresses it gives out are fetched by Musizerr itself, not by the
  provider.

No folders, no raw TCP, no declared resource limits (the gateway's defaults apply).

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh navidrome
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs navidrome
```
