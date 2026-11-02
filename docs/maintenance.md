# Data, updates and troubleshooting

## Data and backups

Everything lives in the `/config` volume:

| Path | Contents |
|---|---|
| `manager.sqlite3` | Users, settings, config and game metadata, the progress history |
| `configs/` | Player YAMLs, one folder per user |
| `games/<id>/` | Multiworld data, the server's save file (`multidata.apsave`), patch files, log |
| `games/<id>/backups/` | Copies of the save (see below) |
| `apworlds/` | The custom world library |
| `jobs/` | Generation inputs and logs (older ones are pruned at startup, keeping the latest 50) |
| `signups/<id>/` | The YAMLs entered in a sign-up round |
| `ap-data/Archipelago/` | Archipelago's `host.yaml`, logs and the custom worlds folder |
| `ap-versions/<version>/` | A further Archipelago version: its source with its own `host.yaml` and custom worlds, and its Python environment |

Back up the whole folder. Game saves are written every minute and when a server stops, so stopping the container
(`docker stop`, allow 30 s) saves everything. Without stopping anything, *Admin → Settings → Backup of everything*
downloads the folder as one zip, with a consistent copy of the database (`tmp/`, the generated templates and the
further Archipelago versions are left out: after a restore, *Install again* brings a version back). To restore it, stop the container, put the zip's contents into an empty data folder, and start the container.
The zip holds the password hashes, the Discord client secret and the webhook addresses: keep it as safe as the folder
itself. The copies under `games/<id>/backups/` protect against mistakes inside
the manager (a reset, a restore); they live in the same folder, so they are no replacement for backing it up.

## Updating

The image bundles one Archipelago release (shown under Admin → Settings → System). A seed records the oldest server
version it needs; if the bundled Archipelago is older than that, the UI refuses the upload with a clear message
instead of failing later.

Archipelago is part of the image, so a new version of the image's Archipelago arrives as a new image. Games that should
stay on an older (or try a newer) Archipelago can run on a further version installed from the web UI (see
*Archipelago versions* above); those stay as they are when the image is updated.

**On the server:** when a newer Archipelago release exists, admins see a notice on the dashboard and under
*Admin → Settings → System* (the manager asks GitHub once at start and then twice a day; `UPDATE_CHECK=false` turns
that off). To update, pull the newer image: on Unraid, *Docker → Check for updates → Apply*. Users, configs, games and
saves stay, and games that were running start again. Custom APWorlds can stop loading on a new Archipelago version;
*Custom games* and the System page show which.

**Where new images come from:** the `Archipelago update` workflow checks for a new release every day. When there is
one, it opens a pull request that bumps `AP_VERSION` in the `Dockerfile` and starts CI for it, which tests the manager
against the new release and smoke tests the image, on amd64 and on arm64. Merging the pull request publishes the new
image. This needs one
setting in the repository: *Settings → Actions → General → Workflow permissions → Allow GitHub Actions to create and
approve pull requests*. You can also run the workflow by hand (*Actions → Archipelago update → Run workflow*) or edit
`AP_VERSION` yourself.

**Building it yourself:** `docker build --build-arg AP_VERSION=<release> .`

## Troubleshooting

- **A game says "port already in use" or "No free port".** Another container or service holds the port, or the servers
  of as many games as `PORT_RANGE` has ports are running. Stop a game (its port then goes to the next game that needs
  one), let games fall asleep, pick a different port in the game's settings, or widen the range (and the published
  ports).
- **Players cannot connect.** Check the address under *Connect* on the game page (set *Public address*), that the port is
  published by Docker and forwarded by your router, and that the game is *Running*.
- **"Not installed on this server" for a game.** Add the APWorld under *Custom games*. After installing, existing
  configs are re-checked automatically.
- **A custom world shows "Not loaded".** The tooltip and the System page show the reason, usually an incompatible
  Archipelago version. A file that is not a valid zip is moved to `quarantine/` in the data folder, because
  Archipelago refuses to start with such a file in its worlds folder.
- **A page shows "Loading…" for a long time.** The container's log names every request that took 5 seconds or more
  (`Slow request: GET /api/games took 42.0 s`), so you can see which one held the page up. On Unraid, a data folder on
  the array is slow to read when its disk has spun down, and `/mnt/user` is slower than a pool for the many small files
  the manager reads: keep appdata on a cache pool.
- **Generation fails.** The job page shows the generator's output and the relevant error. A missing or wrong base ROM
  is explained in plain words; see [Games that need a base ROM](custom-games.md#games-that-need-a-base-rom).
- **Edits to `host.yaml` are lost.** That happens when running as root (`PUID=0`); use a regular user.
- **Messages in the browser's console that are not the manager's.** The pages have no inline scripts and the manager
  sends no `Permissions-Policy` header, so these come from elsewhere:
  - *Error with Permissions-Policy header: Unrecognized feature: 'browsing-topics'* (or `attribution-reporting`,
    `join-ad-interest-group`, `run-ad-auction`, `private-aggregation`): a reverse proxy or CDN in front of the manager
    adds that header, naming features browsers have dropped. Compare `curl -sI http://<unraid-ip>:8080/` with
    `curl -sI https://<your-address>/` and remove the names (or the header) in the proxy's settings.
  - *Content-Security-Policy: The page's settings blocked an inline script*: a browser extension tried to put a script
    into the page, and the page's policy stopped it, as it should. In a private window without extensions the message
    is gone.
  - *MaxListenersExceededWarning: Possible EventEmitter memory leak detected*: written by bundled code of a browser
    extension (the manager has no such code); the same private window shows that.
