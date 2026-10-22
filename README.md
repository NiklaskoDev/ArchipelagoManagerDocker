# Archipelago Manager

A Docker container, built for [Unraid](https://unraid.net) but happy on any Docker host, that hosts, generates and
distributes [Archipelago](https://archipelago.gg) multiworld randomizers through a web UI. Admins run the server;
players log in to keep their YAML configs, see their games and download their patch files.

## What it does

- **Runs several multiworlds at once.** Every game gets its own server process on its own port, with its own
  password and rules. Games that were running come back after a container restart; a crashed server is restarted.
- **Hosts pregenerated games.** Upload the zip a generator produced (or just the `.archipelago` file). The patch files
  are extracted and handed to the players they belong to, matched by slot name. YAMLs bundled in the zip are copied
  into the library of the user who owns the slot.
- **Stores configs per user.** Everyone has a private library of player YAMLs: upload one, or create it in a form with
  every option of the game (custom games included), like the player options page on archipelago.gg. The YAML can be
  edited directly too, or started from the documented template.
- **Generates games.** Pick configs, choose spoiler level, seed, race mode, plando and an optional `meta.yaml`, and
  generate with the real `Generate.py`. The result is imported as a game, with slots linked to the configs' owners, and
  can be hosted straight away. Jobs queue, show a live log and can be cancelled.
- **Runs sign-up rounds.** An admin opens a round with a deadline; players enter their YAMLs, which are checked on the
  spot. When the deadline passes the game is generated from all entries and everyone is added to it.
- **Posts to Discord.** A webhook message when a game goes online or crashes, a player reaches their goal, a hinted
  item is found, a game is generated, sign-ups open or close, a custom game waits for approval, or a new Archipelago
  version is out.
- **Keeps custom games.** `.apworld` files are stored. If a config or a seed needs a game the server does not have, the
  UI says so and offers to add it. Regular users can *submit* an APWorld; an admin approves it.
- **One UI for admins and players.** Players see only their games, can start and stop them (if allowed) and download
  their files. Admins also get the live console, file management, per-game settings and user management.

## Quick start

### Unraid

1. Add the template. Until the app is listed in Community Applications, put it where Unraid looks for user templates
   (Terminal, or the Unraid web terminal):

   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/archipelago-manager.xml \
     https://raw.githubusercontent.com/NiklaskoDev/ArchipelagoManagerDocker/main/unraid/archipelago-manager.xml
   ```

2. **Docker** tab → **Add Container** → choose **ArchipelagoManager** under *User templates*.
3. Check the settings, then **Apply**:
   - **Data (appdata)**: where everything is stored. Back this folder up.
   - **Game ports**: one port per hosted game; the default range holds 20 games at once. If you change it, change
     *Game port range* to the same values.
   - **Public address**: the name or IP your players type (see [Reaching your games](#reaching-your-games)).
4. Open the **WebUI** from the container's menu and create the administrator account. For safety this works only on
   the server's local address (its IP address or local name, as the WebUI button opens it), not through a domain name.

The image runs as Unraid's `nobody:users` (99:100) by default, so the appdata folder stays accessible from your shares.

### Docker Compose

```sh
git clone https://github.com/NiklaskoDev/ArchipelagoManagerDocker.git
cd ArchipelagoManagerDocker
docker compose up -d        # uses the published image; set PUID/PGID/PUBLIC_HOST in docker-compose.yml first
```

Open <http://localhost:8080>. To build the image yourself instead, uncomment `build: .` in `docker-compose.yml`.

### Reaching your games

Every game listens on its own port, and Archipelago clients connect straight to it, so these ports must be reachable
by your players:

- **Same network:** nothing to do. Leave *Public address* empty and players use the address of the web UI's host.
- **Over the internet:** forward the whole game port range (TCP) from your router to the server, and set *Public
  address* (Admin → Settings) to your domain or WAN address. The web UI port does not need to be exposed unless you
  want players to log in remotely. If you do, put it behind a reverse proxy with HTTPS, and set
  `FORWARDED_ALLOW_IPS` to the proxy's address: otherwise every visitor seems to come from the proxy and shares its
  limit of failed logins (the log says so when it happens). Also set *Address of this web UI* (Admin → Settings) to the
  `https://` address; cookies are then always marked secure.
- **Host networking:** works too and avoids publishing ports. Then set `PORT` to a free port for the web UI.

## Using it

### As a player

1. **My configs** → upload your YAML, or *New config* and pick your game: its options open as a form (see
   [The options form](#the-options-form)). The editor checks the file when you save and tells you what is wrong. The
   player name is the slot name in the game.
2. When an admin generates a game with your config, or uploads a game with a slot of that name, it appears under
   **Games** with the server address, password, your slot name and your files. If the game allows it you can *claim*
   a slot yourself ("This is mine").
3. Download your patch file(s), patch your game, and connect with the Archipelago client of your game.
4. While you play, **Your tracker** on the game page shows the items you received (and from whom), the locations you
   still have to check, and your hints: where your items are, and which items of other players are in your world. It
   is read from the server's save, so it can be up to a minute behind.

**Sign-ups:** open rounds are on the dashboard and under **Sign-ups**. *Join* → pick one of your configs or upload a
YAML. It is checked against the round's rules right away (allowed games, unique player names, how many configs each
player may enter). Your entry is a copy: if you edit the config afterwards, the round page offers to use the new version.
You can withdraw until the round closes.

#### The options form

Every config opens in two tabs: **Options**, a form with one field per option of the game, and **YAML**, the file
itself. Both edit the same file, so switch whenever you like.

- The fields come from Archipelago: the same options, groups, descriptions, defaults and limits as the player options
  page on archipelago.gg, for every installed game. That includes **custom games**: an APWorld's own options show up as
  soon as it is installed.
- Choices, numbers (with their named values such as *Normal (50)*), on/off switches, your own text where a game takes
  one (a boss plando in A Link to the Past, say), items and locations to pick by name (local items, start inventory,
  excluded locations, …). *Random* lets Archipelago choose when it generates. *Find an option* narrows a long form down,
  and a game's own presets set everything at once.
- What the form cannot show stays in the file as it is: weights over several values (`full: 50, minimal: 50`), random
  ranges, plando, triggers and keys it does not know. The form says which. Only when you change something in the form
  is the file written anew, and then comments in it are not kept; edit in the YAML tab if they matter to you.
- A file with several players, or one whose game is picked at random, opens as YAML only.
- *Start from the documented template instead* (YAML tab of a new config) gives the template with every option
  explained in comments.

### As an admin

- **Add a pregenerated game:** *Games → Add a game*, drop the zip. The slots are matched to users whose config has the
  same player name (case-insensitive) and game. If the zip contains the players' YAMLs, a user whose config is one of
  them wins over others with the same name. If it is still ambiguous the slot is left unassigned rather than guessed;
  assign it from the slot's *Owner* drop-down. Turning on *claimable* in the game's settings lets players pick their own;
  add *only one slot each* so nobody can take more than one (you can still assign extra slots by hand).
- **Generate a game:** *Generate*, tick the configs, set the options, *Generate*. Slot owners are taken from the configs.
- **Run a sign-up round:** *Sign-ups → New round*: a name, a deadline (or none, to close it by hand), optionally the
  allowed games and how many configs each player may enter, and the generation options. At the deadline the sign-ups
  close and the game is generated and started; switch that off to generate by hand. If generating fails (for example a
  game is not installed), the round stays closed with the error: fix the entries, then *Generate* again or *Reopen*.
- **Discord notifications:** *Admin → Settings → Discord notifications*. In Discord, open the channel's settings →
  *Integrations* → *Webhooks* → *New Webhook* → *Copy Webhook URL*, paste it, and *Send a test message*. Set *Address of
  this web UI* (under *General*) so messages link to the right page, pick the language of the messages (English or
  German), and untick the kinds of messages you do not want. Goals and found
  hinted items are read from the server's save, so they are posted up to a minute after they happen. Many hinted items
  found at once (after a `!release` or `!collect`) come as one message.
- **Sign in with Discord:** *Admin → Settings → Sign in with Discord*.
  1. Set *Address of this web UI* under *General* to the address players open, e.g. `https://archipelago.example.com`.
  2. In the [Discord Developer Portal](https://discord.com/developers/applications): *New Application*, then under
     *OAuth2 → Redirects* add the *Redirect address* shown on the settings page (the web UI's address followed by
     `/api/auth/discord/callback`).
  3. Copy *Client ID* and *Client Secret* from *OAuth2* into the settings, tick *Let people sign in with Discord* and
     save. The secret is stored on the server and never shown again.

  Players then link their Discord account in the user menu (*Link Discord*) and can sign in with one click. Tick
  *Create an account…* to let people without an account sign up through Discord (they can set a password later in the
  user menu), and enter a Discord server ID to admit only members of that server. Accounts are matched by Discord's
  account ID only, never by name; admins see who is linked under *Admin → Users* and can unlink an account there.
- **Run a game:** *Start / Stop / Restart* on the game page. *Settings* holds the room password, the remote-admin
  password, hint cost, `!release`/`!collect`/`!remaining` rules and the idle shutdown. Passwords and rules apply to a
  running server immediately; the port and a few startup options need a restart (the UI tells you).
- **Console:** every server's log, plus a command line for Archipelago's server commands (`/send`, `/hint`, `/help`, …).
- **Progress:** per slot checks and whether a player is online, read from the server's save (refreshed about once a
  minute while the game is running). The tracker on the game page lets you pick any slot and see its items, locations
  and hints.
- **Backups of a game's progress:** *Backups* on the game page. While the save changes, a copy is kept every 30 minutes
  (the latest 48, about a day of play), plus one before every *Reset progress* and every restore, and whenever you
  press *Back up now* (the latest 20 of those). *Restore* puts a copy back while the server is stopped, after keeping
  the current progress as a copy, so a reset or a restore can always be undone.
- **Users and settings:** *Admin* has users, registration, what players may do, defaults for new games, Archipelago's
  `host.yaml`, and system information.

### Language

The web UI is available in English and German. It follows the browser's language at first; switch it on the sign-in
page or in the user menu (the choice is remembered in that browser).

### Custom games (APWorlds)

Games that are not part of Archipelago itself come as `.apworld` files.

- An admin uploads one under **Custom games**. It is installed immediately and available for generating, and players
  can create configs for it in the [options form](#the-options-form) with the world's own options.
- When a config or an uploaded seed needs a game that is not installed, a prompt says which one and offers to add it.
  *Hosting a seed does not need the APWorld* (the seed carries what the server needs), but players need it to patch and
  play, so installed APWorlds can be downloaded from the same page.
- Users can *submit* an APWorld; it waits for an admin to approve it (switch this off under Admin → Settings).
- Files placed by hand in Archipelago's worlds folder are adopted automatically at startup.
- **From GitHub:** many APWorlds are published as releases of a GitHub repository. *Custom games → From GitHub*, paste
  the repository (`owner/name` or its address) and *Look up*: it shows the newest release and its `.apworld` file.
  - A repository a **player** adds waits for an administrator, like a submitted file. Nothing is downloaded from it
    until it is approved; *Approve* downloads the newest release and installs it. Discord can announce waiting ones.
  - A repository an **admin** adds is installed right away.
  - Approved repositories are checked for new releases twice a day (or with the check button). A new release shows up
    with an *Update* button and can be announced on Discord, but it is never installed on its own: a new release is new
    code, so an admin decides. Removing a repository from the list keeps the game installed.

> **An `.apworld` is code that runs on your server.** Only install files from sources you trust. This is why users
> can only submit, never install.

### Games that need a base ROM

Some games need their original ROM on the server to *generate* (for example A Link to the Past, Ocarina of Time,
Super Mario World, Secret of Evermore, The Legend of Zelda); others only make a patch file and do not. Hosting a
pregenerated seed never needs a ROM, and players always need their own to patch.

The manager asks each game whether its ROM is in place, using the same check Archipelago runs before generating. When
one is missing, the generate page, the config and the sign-up round say which file is needed and where it goes, and
generating is refused right away instead of failing in the queue (or at a round's deadline). Put the ROM into
`ap-data/Archipelago/` in the data folder (on Unraid `/mnt/user/appdata/archipelago-manager/ap-data/Archipelago/`) under
exactly the name shown, or point the game's entry in `host.yaml` (Admin → Settings) at it. The warnings disappear as
soon as the file is there. Some games also check that it is the right dump (region and revision); a wrong one fails
with a message that says so.

## Configuration

Everything is optional except the volume. Set these as environment variables (Unraid: the template's fields).

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | User and group the manager and its game servers run as. `0` runs as root (not recommended). |
| `UMASK` | `022` | File creation mask. |
| `PORT` | `8080` | Port of the web UI inside the container. |
| `PORT_RANGE` | `38281-38300` | Ports handed out to games. Publish exactly this range. |
| `PUBLIC_HOST` | *(empty)* | Address players use. Empty = the address the web UI was opened with. Also editable in the UI. |
| `ADMIN_USER` / `ADMIN_PASSWORD` | *(empty)* | Create the first administrator on start instead of showing the setup page. |
| `FORWARDED_ALLOW_IPS` | *(empty)* | Reverse proxy addresses whose `X-Forwarded-*` headers are trusted. |
| `MAX_UPLOAD_MB` | `512` | Largest upload accepted. |
| `MAX_EXTRACT_MB` | `2048` | Largest total size a game archive may unpack to. |
| `GENERATION_TIMEOUT` | `3600` | Seconds before a generation is stopped. |
| `TZ` | *(empty)* | Time zone for logs. |
| `UPDATE_CHECK` | `true` | Ask GitHub for new releases of Archipelago and of custom games added from GitHub, and show them to admins. `false` stops all requests the manager makes to GitHub on its own. |

## Data and backups

Everything lives in the `/config` volume:

| Path | Contents |
|---|---|
| `manager.sqlite3` | Users, settings, config and game metadata |
| `configs/` | Player YAMLs, one folder per user |
| `games/<id>/` | Multiworld data, the server's save file (`multidata.apsave`), patch files, log |
| `games/<id>/backups/` | Copies of the save (see below) |
| `apworlds/` | The custom world library |
| `jobs/` | Generation inputs and logs (older ones are pruned at startup, keeping the latest 50) |
| `signups/<id>/` | The YAMLs entered in a sign-up round |
| `ap-data/Archipelago/` | Archipelago's `host.yaml`, logs and the custom worlds folder |

Back up the whole folder. Game saves are written every minute and when a server stops, so stopping the container
(`docker stop`, allow 30 s) saves everything. The copies under `games/<id>/backups/` protect against mistakes inside
the manager (a reset, a restore); they live in the same folder, so they are no replacement for backing it up.

## Updating

The image bundles one Archipelago release (shown under Admin → Settings → System). A seed records the oldest server
version it needs; if the bundled Archipelago is older than that, the UI refuses the upload with a clear message
instead of failing later.

Archipelago is part of the image, so a new Archipelago version arrives as a new image. Nothing is installed inside a
running container.

**On the server:** when a newer Archipelago release exists, admins see a notice on the dashboard and under
*Admin → Settings → System* (the manager asks GitHub once at start and then twice a day; `UPDATE_CHECK=false` turns
that off). To update, pull the newer image: on Unraid, *Docker → Check for updates → Apply*. Users, configs, games and
saves stay, and games that were running start again. Custom APWorlds can stop loading on a new Archipelago version;
*Custom games* and the System page show which.

**Where new images come from:** the `Archipelago update` workflow checks for a new release every day. When there is
one, it opens a pull request that bumps `AP_VERSION` in the `Dockerfile` and starts CI for it, which tests the manager
against the new release and smoke tests the image. Merging the pull request publishes the new image. This needs one
setting in the repository: *Settings → Actions → General → Workflow permissions → Allow GitHub Actions to create and
approve pull requests*. You can also run the workflow by hand (*Actions → Archipelago update → Run workflow*) or edit
`AP_VERSION` yourself.

**Building it yourself:** `docker build --build-arg AP_VERSION=<release> .`

## Security notes

- Passwords are stored as salted scrypt hashes; sessions are `HttpOnly`, `SameSite=Lax` cookies; state-changing API
  calls require a custom header, which blocks cross-site requests; failed logins are rate limited.
- Archipelago's game connections are plain websockets and are not encrypted. A room password only keeps people out,
  it is sent unencrypted. Do not reuse a password you care about.
- The web UI speaks plain HTTP. Do not expose it to the internet without an HTTPS reverse proxy.
- Spoiler logs, bundled YAMLs and other players' files are visible to admins only, unless an admin shares them.
- The console is admin-only and runs real server commands. Remote admin (`!admin`) is off unless you set a password.
- Discord messages never contain passwords and cannot mention anyone (`@everyone` in a player name stays plain text).
  Treat the webhook address like a password: anyone who has it can post to the channel.
- Signing in with Discord uses OAuth2 with a single-use `state` bound to the browser that started it, a fixed redirect
  address, and Discord's account ID; Discord's tokens are revoked right after use. Disabled users cannot sign in this
  way either.
- Server logs never show the room or admin passwords (MultiServer repeats them when they are set).
- Without a session, requests may be only a few kilobytes; only admins (and, for custom games, players allowed to
  submit them, up to 100 MB) can upload large files. Players can keep at most 200 configs and have at most 5 custom
  games and 5 GitHub repositories waiting for approval.
- An approved GitHub repository installs exactly the release file the admin was shown; if the repository changed it in
  the meantime, the admin is asked to look again. Downloads are checked against GitHub's SHA-256 digest.
- The administrator account can only be created on a local address, so a website cannot take over a fresh install
  through DNS rebinding.

## Troubleshooting

- **A game says "port already in use" or "No free port".** Another container or service holds the port, or all ports of
  `PORT_RANGE` are taken by games. Pick a different port in the game's settings, or widen the range (and the published
  ports).
- **Players cannot connect.** Check the address under *Connect* on the game page (set *Public address*), that the port is
  published by Docker and forwarded by your router, and that the game is *Running*.
- **"Not installed on this server" for a game.** Add the APWorld under *Custom games*. After installing, existing
  configs are re-checked automatically.
- **A custom world shows "Not loaded".** The tooltip and the System page show the reason, usually an incompatible
  Archipelago version. A file that is not a valid zip is moved to `quarantine/` in the data folder, because
  Archipelago refuses to start with such a file in its worlds folder.
- **Generation fails.** The job page shows the generator's output and the relevant error. A missing or wrong base ROM
  is explained in plain words; see [Games that need a base ROM](#games-that-need-a-base-rom).
- **Edits to `host.yaml` are lost.** That happens when running as root (`PUID=0`); use a regular user.

## Development

```sh
# run the manager against a local Archipelago checkout
git clone --depth 1 --branch "$(sed -n 's/^ARG AP_VERSION=//p' Dockerfile)" https://github.com/ArchipelagoMW/Archipelago.git
python -m venv ap-venv && ap-venv/bin/pip install -r <(grep -v -i -E '^(kivy|kivymd|pyshortcuts)' Archipelago/requirements.txt) "setuptools>=75,<81" requests
for r in Archipelago/worlds/*/requirements.txt; do ap-venv/bin/pip install -r "$r"; done   # as the Dockerfile does

pip install -r tests/requirements.txt
cd manager
DATA_DIR=../data AP_DIR=$PWD/../Archipelago AP_PYTHON=$PWD/../ap-venv/bin/python PORT=8080 \
  python -m archipelago_manager
```

- **Tests:** `AP_DIR=… AP_PYTHON=… pytest`. They start the real server and talk to it over HTTP and websockets,
  generate real multiworlds and connect real clients. Without `AP_DIR` the tests that need Archipelago are skipped.
- **Browser walkthrough:** `tests/ui/flows.mjs` drives the UI with Playwright (see the file header).
- **Image smoke test:** `python tests/smoke.py http://localhost:8080` against a fresh instance. CI runs it against the
  built image before publishing.
- The front end is plain ES modules in `manager/archipelago_manager/static/`, with no build step.
- `ap_helper.py` runs inside Archipelago's own Python environment: it lists worlds, decodes multidata and saves,
  renders templates and describes each game's options for the options form, so the manager never imports Archipelago's
  code itself. `yamlform.py` turns a YAML into the form's values and back.

## Limitations

- One Archipelago version at a time; images are published for `linux/amd64`.
- Game ports are served by Archipelago's own server and are not behind TLS.
- Base ROMs are not uploaded through the web UI: copy them into the data folder (see
  [Games that need a base ROM](#games-that-need-a-base-rom)).
