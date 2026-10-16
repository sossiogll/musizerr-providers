# Local storage

Keeps the library in a folder of the machine Musizerr runs on. A storage provider: it stores each downloaded file
under the library folder, plays it from there and lists what is on it.

- **Store** moves the file from the call's workspace into the library (a rename when they are on one volume, a
  copy otherwise), creating the folders it needs.
- **Stream** puts the library file in the workspace (a hard link, or a copy across volumes) for the host to serve;
  the library is never touched.
- **List** walks the folder; a link to a folder is listed as a file, not followed, so a circular link cannot stall it.
- A path that is absolute, or that climbs out of the folder, is refused: the check is lexical and does not follow
  links, because a library may use them on purpose.

## Settings

| Key | Meaning |
| --- | --- |
| `root_dir` | The library folder (default `/downloads`). |

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- **read and write the folder you set in `root_dir`** (the library). The gateway binds that folder into the
  sandbox at the same path, and only if it lies inside the folders the gateway is allowed to grant
  (`GATEWAY_FILESYSTEM_ROOTS`); files it creates there belong to the gateway's user, so the folder must be writable
  by it. Nothing else of the disk is visible.

No network, no raw TCP, no declared resource limits (the gateway's defaults apply).

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh local
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs local
```
