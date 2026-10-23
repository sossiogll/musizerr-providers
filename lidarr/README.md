# Lidarr (download)

A download provider that does not download anything itself: it asks **your Lidarr server** to get the album a
track belongs to, and follows the import until the files are in the library.

- For a track it first asks MusicBrainz for the release group of the recording (Lidarr only accepts those), then
  looks the album up in Lidarr and adds it (monitored, with the first quality and metadata profile and root folder
  of your Lidarr). An album already there is searched again instead of added; an album Lidarr already holds
  complete, without the requested track, is reported as such and not searched.
- For a whole album (`dispatch_album`) it does the same by album title and artist.
- The download is **delegated**: Musizerr follows it by polling Lidarr (queue, history, files on disk), and
  Lidarr can tell Musizerr when it is done (the provider registers a Webhook connection in Lidarr, idempotently).
- Registering the Webhook can fail, and then it says why. Lidarr tests the address when it saves the connection and
  refuses (400, "Unable to send test message") when it cannot reach it; the provider raises with Lidarr's own message
  (and for a refused key, an error of Lidarr's or a Lidarr that does not answer, the matching reason), never an empty
  answer. The callback address must be reachable **from Lidarr**: with both in the same Docker network, use an
  internal URL such as `http://frontend` as Musizerr's public URL. The callback token is never shown in a message or
  a log.
- It is a last-resort provider and **off by default** (`enabled_by_default = False`).

## Settings

| Key | Meaning |
| --- | --- |
| `url` | The base URL of your Lidarr instance (for example `http://localhost:8686`). Required. |

The API key is the provider's credential (one for the instance, managed by an admin); it travels in the
`X-Api-Key` header, never in a URL.

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- reach `musicbrainz.org` on port 443 (HTTPS), to find the release group of a track;
- reach the server the setting `url` names, on the port of that URL (its scheme's default port when it names
  none). The destination is allowed while the setting holds it, private and Tailscale addresses included, because
  you typed it.

No folders, no raw TCP, no declared resource limits (the gateway's defaults apply).

## In the sandbox

An address that does not respond is not a connection error there: the gateway's egress proxy answers a plain-HTTP request
to it with `502 Bad Gateway`. The provider treats a 502, 503 or 504 like a connection error (`LidarrUnreachable`: the
host retries later) and not as Lidarr saying "no" (a definitive "not in the catalog"), which is also the right reading
of those statuses from a reverse proxy in front of Lidarr. Any other status is still Lidarr's own answer. This is the
only change in behaviour since it lived in the application.

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh lidarr
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs lidarr
```
