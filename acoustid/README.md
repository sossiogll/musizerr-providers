# AcoustID (identification)

Tells what an audio file **really is** from its sound, not from its tags (which can be wrong): it fingerprints the
file with [Chromaprint](https://acoustid.org/chromaprint) and asks the [AcoustID](https://acoustid.org) database
for the recording. Musizerr uses it after a download to catch a mislabelled file and to find true duplicates.

The provider returns its best match with the raw score and only the **title and artist** (plus the MusicBrainz
recording id): album and year are not reliable from a fingerprint, so they are not returned. Whether a score is
good enough is the host's decision (`identification_confidence_threshold`), not the provider's.

It is **inert until you give it a key**: without an API key it is registered but reports itself unavailable and
asks nobody.

## Settings

| Key | Meaning |
| --- | --- |
| `api_key` | Your AcoustID application API key (free, from [acoustid.org](https://acoustid.org/new-application)). Secret. Required. |
| `rate_limit_per_sec` | Requests per second to the AcoustID API (optional). |

## Inside the image

`fpcalc`, the Chromaprint program that computes the fingerprint, is installed in the image (Debian package
`libchromaprint-tools`) and runs inside the sandbox, next to the provider; the Python side is
[`pyacoustid`](https://github.com/beetbox/pyacoustid) 1.3.0. This makes the image larger than the others (it
carries the audio decoding libraries Chromaprint needs).

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- reach `api.acoustid.org` on port 443 (HTTPS). What travels is the fingerprint, the duration and your API key.
  `pyacoustid` 1.3.0 defaults to plain HTTP (`http://api.acoustid.org/v2/`), which would send the key in the clear,
  so the provider sets `https://api.acoustid.org/v2/` on the library when it is imported (the library's own
  `set_base_url`), and passes the context's HTTP timeout to every request (the library waits forever otherwise).

No folders, no raw TCP, no declared resource limits (the gateway's defaults apply). The file to identify is read
from the call's workspace; `fpcalc` needs no network.

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh acoustid        # installs fpcalc in the test container (tests/apt-packages.txt)
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs acoustid
```
