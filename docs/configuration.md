# Configuration

How the container is set up: its environment variables, the address players connect to, and encrypted game connections.

Everything is optional except the volume. Set these as environment variables (Unraid: the template's fields).

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | User and group the manager and its game servers run as. `0` runs as root (not recommended). |
| `UMASK` | `022` | File creation mask. |
| `PORT` | `8080` | Port of the web UI inside the container. |
| `PORT_RANGE` | `38281-38300` | Ports handed out to games. Publish exactly this range. |
| `PUBLIC_HOST` | *(empty)* | Address players use. Empty = the address the web UI was opened with. Also editable in the UI. |
| `ADMIN_USER` / `ADMIN_PASSWORD` | *(empty)* | Create the first administrator on start instead of showing the setup page (which asks for the setup code from the log). |
| `FORWARDED_ALLOW_IPS` | *(empty)* | Reverse proxy addresses whose `X-Forwarded-*` headers are trusted. |
| `MAX_UPLOAD_MB` | `512` | Largest upload accepted. |
| `MAX_EXTRACT_MB` | `2048` | Largest total size a game archive may unpack to. |
| `GENERATION_TIMEOUT` | `3600` | Seconds before a generation is stopped. |
| `TZ` | *(empty)* | Time zone for logs. |
| `AP_SOURCE_URL` | GitHub | Where further Archipelago versions are downloaded from; `{version}` stands for the release number. Default `https://github.com/ArchipelagoMW/Archipelago/archive/refs/tags/{version}.tar.gz`. |
| `TLS_CERT` / `TLS_KEY` | *(empty)* | Certificate and key for [encrypted game connections](#encrypted-game-connections), as paths inside the container. Also editable in the UI. |
| `UPDATE_CHECK` | `true` | Ask GitHub for new releases of Archipelago and of custom games added from GitHub, and show them to admins. `false` stops all requests the manager makes to GitHub on its own. |

## Reaching your games

Every game listens on its own port, and Archipelago clients connect straight to it, so these ports must be reachable
by your players:

- **Same network:** nothing to do. Leave *Public address* empty and players use the address of the web UI's host.
- **Over the internet:** forward the whole game port range (TCP) from your router to the server, and set *Public
  address* (Admin → Settings) to your domain or WAN address. The web UI port does not need to be exposed unless you
  want players to log in remotely. If you do, put it behind a reverse proxy with HTTPS, and set
  `FORWARDED_ALLOW_IPS` to the proxy's address: otherwise every visitor seems to come from the proxy and shares its
  limit of failed logins (the log says so when it happens). Also set *Address of this web UI* (Admin → Settings) to the
  `https://` address. Signing in there marks the cookie secure (sent over https only), behind the proxy or tunnel too;
  signing in on the local network's plain `http://` address works as well, with a cookie that is not, because a browser
  drops a secure cookie that comes over http.
- **Host networking:** works too and avoids publishing ports. Then set `PORT` to a free port for the web UI.

## Encrypted game connections

Archipelago's game connections are unencrypted websockets unless the server has a certificate. Give the manager one,
and every game server accepts encrypted connections (`wss://`) only, so room passwords and chat no longer cross the
internet readable.

1. Get a certificate for the *Public address* from an authority the clients trust, such as Let's Encrypt. If a reverse
   proxy already gets one for that name (SWAG, Nginx Proxy Manager, Caddy, Traefik), use that one: mount its folder
   into the container, read only, e.g. `/mnt/user/appdata/swag/etc/letsencrypt:/certs:ro` (Unraid: the template's
   *Certificates* path).
2. *Admin → Settings → Encrypted game connections*: enter the certificate (`fullchain.pem`, with its chain) and the
   key (`privkey.pem`, without a password) as paths inside the container, such as `/certs/live/example.com/fullchain.pem`.
   Or set `TLS_CERT` and `TLS_KEY`. The manager loads them on saving and refuses files a server could not use; the
   card shows the names the certificate is for, until when it is valid, and warns if it is not for the public address
   or is self-signed (which Archipelago's clients refuse).

Players change nothing: Archipelago's own clients try `wss://` by themselves when the plain connection gets no answer,
and the game page marks the address *encrypted*. A client that cannot find out by itself takes the address with
`wss://` in front. A client that cannot encrypt at all cannot connect to such a game: turn off *Encrypted connections
(wss)* in that game's settings (or under *Defaults for new games*).

- **Renewing:** a server reads the certificate when it starts. When the files change, every running server that still
  presents the old certificate restarts once nobody is connected to it (or right away, if its certificate has
  expired); the same happens when encryption is turned on or off. Admins see a warning on the dashboard two weeks
  before the certificate expires.
- **When the certificate cannot be used** (deleted, unreadable for `PUID`, not matching its key), games with encrypted
  connections do not start, and say why, rather than starting unencrypted. Running ones keep the certificate they have.
- The files need to be readable by the user the manager runs as (`PUID`). Some proxies keep the key readable for root
  only; then copy the two files into the data folder (`/config/tls/`) after each renewal instead.
