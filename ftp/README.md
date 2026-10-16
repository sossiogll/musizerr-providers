# FTP storage

Keeps the library on an FTP server. A storage provider: Musizerr stores each downloaded file there, plays it
straight from the server and lists what is on it.

- **Store** creates the folders it needs and uploads the file; **exists**, **size** and **delete** are what they say.
- **Stream** hands Musizerr the bytes as they arrive from the server (the first note plays at once, not after the
  whole file), and a seek restarts the transfer at the offset (FTP `REST`): it declares `supports_stream_offset`.
- **List** walks the base path (MLSD, or NLST on a server that does not know MLSD) so the library scan can find what
  is already there.
- **Passive mode only.** It is the default of an FTP client and the only one that can work from a sandbox (active
  mode asks the server to connect back).

## Settings

| Key | Meaning |
| --- | --- |
| `host` | The server's name or address. Required. |
| `port` | The control port (default 21). |
| `remote_base_path` | The folder of the library on the server, for example `/music`. Required. |

The login (user and password) is the provider's credential, managed by an admin.

## Permissions

What the sandbox lets it do, and nothing else (the admin sees this before installing):

- open raw TCP connections to **the host you set in `host`, on any port**. FTP uses one connection for the commands
  and a new one for every transfer, and in passive mode the server picks that port itself each time, so a fixed
  port or range cannot be declared. The host is the one you typed (a private or Tailscale address included); it can
  never be anything else, and loopback and link-local addresses are never reachable.

No other network, no folders, no declared resource limits (the gateway's defaults apply). The library
(`musizerr_sdk.net`) routes the sockets of `ftplib`, the control one and every passive data one, through the
gateway's proxy; outside a sandbox that is the ordinary socket call.

## Build and test

From the root of the repository (see the top-level [README](../README.md)):

```sh
scripts/test.sh ftp      # includes a real FTP server in process, direct and through a CONNECT proxy
scripts/build.sh --registry 127.0.0.1:18800 --catalog-dir /tmp/catalogs ftp
```
