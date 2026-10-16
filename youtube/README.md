# YouTube (likes only)

Imports the videos a user **liked** on YouTube and turns them into Musizerr favorites. A metadata provider
with no catalog:

- it never searches, and has no albums, artists, charts or related tracks;
- it never plays or downloads YouTube content (YouTube's terms forbid separating the audio);
- it only reads the user's likes, and keeps the videos of the **Music** category (`categoryId` 10). Likes of any
  other category are ignored.

An imported like becomes a favorite through the usual like matching in Musizerr: there is no ISRC on YouTube, so a
like matches a library track by title, artist and duration. The decoration of a video title (`(Official Video)`,
`[HD]`, `- Lyrics`, `(Video Ufficiale)`...) is ignored; a real version (live, remix, acoustic...) is not the
original. The best results come from the auto-generated `Artist - Topic` uploads, whose duration is the track's:
an official video with an intro or an outro is longer than the audio and, beyond the duration tolerance (3 seconds
or 2%), does not match, so the like stays "liked elsewhere".

With the personal setting `unlike_on_origin_providers` on, taking a favorite off in Musizerr also takes the like
off the video on YouTube.

## Settings

None to declare: the OAuth client (id and secret) is saved from the provider's card in the administration.
YouTube is a personal provider: every user connects their own account (the likes are each user's own, there is no
instance account to fall back on).

## What the admin does once

The deployment needs its own Google Cloud project: the YouTube Data API has no public key to share.

1. **Create a project** in the [Google Cloud console](https://console.cloud.google.com/) (or reuse one).
2. **Enable the API**: *APIs & Services → Library → YouTube Data API v3 → Enable*.
3. **Configure the OAuth consent screen**: user type **External**; app name, support and developer e-mail; add the
   scopes `.../auth/youtube.readonly` and `.../auth/youtube` (the second one is only used to take a like off);
   **publishing status: In production** (it can stay *unverified*). In **Testing** mode the refresh token expires
   after 7 days and every user would have to reconnect YouTube every week.
4. **Create the OAuth client**: *APIs & Services → Credentials → Create credentials → OAuth client ID → Web
   application*. As **Authorized redirect URI** put the callback of this deployment, `<APP_BASE_URL>/auth/youtube/callback`
   (`APP_BASE_URL` is the public address Musizerr is served from). Google accepts plain `http` only for
   `localhost`; the URI must match exactly, trailing slash included.
5. In Musizerr, paste the **client ID** and **client secret** in the provider's *App* section; then each user
   connects their account.

An app in production that Google has not verified shows each user a "Google hasn't verified this app" screen on
the first connection, and is capped at **100 users**. This is expected for a self-hosted deployment.

## Quota

The free quota is **10,000 units a day for the whole Google Cloud project**, shared by every user of the instance
(it resets at midnight Pacific time).

| Call | Used for | Cost |
| --- | --- | --- |
| `videos.list` | one page of up to 50 likes, or one video by id | 1 unit |
| `videos.rate` | taking one like off | 50 units |
| `search.list` | never called | 100 units |

Importing likes costs 1 unit per 50 likes, every sync (the pages are read before the Music filter, so every like
counts). When the quota is exhausted YouTube answers 403 `quotaExceeded`; the provider reports it as a limit of the
service for now (never as "no likes") and the next sync retries.

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- reach `www.googleapis.com` on port 443 (HTTPS), the Data API.

The Google sign-in pages (`accounts.google.com`, `oauth2.googleapis.com`) are visited by Musizerr itself when a user
connects, not by the provider, so they are not in its permissions. No folders, no raw TCP, no declared resource
limits (the gateway's defaults apply).

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh youtube
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs youtube
```
