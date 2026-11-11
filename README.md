# Archipelago Manager

A Docker container, built for [Unraid](https://unraid.net) but happy on any Docker host, that hosts, generates and
distributes [Archipelago](https://archipelago.gg) multiworld randomizers through a web UI. Admins run the server;
players log in to keep their YAML configs, see their games and download their patch files.

## What it does

- **Runs several multiworlds at once.** Every game gets its own server process on its own port, with its own
  password and rules. Games that were running come back after a container restart; a crashed server is restarted.
- **Lets games sleep.** A game can fall asleep when nobody has been connected for a while, and wakes up when one of its
  players opens the game page, like on archipelago.gg. Games that are not running lend their port to one that needs it,
  so there can be more games than ports. A game can also be set to never fall asleep.
- **Takes games over from archipelago.gg.** Add the multiworld from its zip and paste the room's address: how far
  everyone got (locations, items, hints, goals) comes along from the room's tracker.
- **Hosts pregenerated games.** Upload the zip a generator produced (or just the `.archipelago` file). The patch files
  are extracted and handed to the players they belong to, matched by slot name. YAMLs bundled in the zip are copied
  into the library of the user who owns the slot.
- **Stores configs per user.** Everyone has a private library of player YAMLs: upload one, or create it in a form with
  every option of the game (custom games included), like the player options page on archipelago.gg. The YAML can be
  edited directly too, or started from the documented template. *Check* asks Archipelago whether the generator would
  take a config, and lists every problem it finds, before anyone generates. Players can share a config with everyone,
  with their friends or with the friends they choose, and the others copy it into their own library with their own
  player name.
- **Friends.** Players add each other by name and answer requests on their dashboard. Friends share configs with each
  other alone, and a player who generates can pick their friends' configs, which puts those friends in the game.
- **Generates games.** Pick configs, choose spoiler level, seed, race mode, plando and an optional `meta.yaml`, and
  generate with the real `Generate.py`. Options you use again (a race setup, an async one) are saved as templates. The result is imported as a game, with slots linked to the configs' owners, and
  can be hosted straight away. Jobs queue, show a live log and can be cancelled. *Generate again* repeats a game or a
  job with the same configs and options and a new seed.
- **Rolls weights, and runs mystery rounds.** *Roll* shows what a config with weights comes out as when the generator
  rolls it. In a mystery round everyone joins with a name and plays what one file of weights rolls for them.
- **Runs sign-up rounds.** An admin opens a round with a deadline; players enter their YAMLs, which are checked on the
  spot. A test generation before the deadline shows whether the entries generate together. When the deadline passes the
  game is generated from all entries and everyone is added to it.
- **Posts to Discord.** A webhook message when a game goes online or crashes, a player reaches their goal or says they
  are stuck, a slot is handed over, a hinted item is found, a game is generated, sign-ups open or close, a custom game waits for approval, or a
  new Archipelago version is out. A game can post to a channel of its own, and players can ask to be mentioned.
- **Keeps custom games.** `.apworld` files are stored. If a config or a seed needs a game the server does not have, the
  UI says so and offers to add it. Regular users can *submit* an APWorld; an admin approves it.
- **Runs every game on the Archipelago version it needs.** Besides the version in the image, admins can install
  further Archipelago releases from the web UI. Each game runs, and each generation generates, on the version chosen for
  it, so an old seed or a custom game that needs another version keeps working next to the rest.
- **An API for scripts and bots.** Everyone can make personal API tokens (read only, or read and change) for a Discord
  bot, a status page or a script, which then use the same API as the web UI in their name.
- **One UI for admins, organizers and players.** Players see only their games, can start and stop them (if allowed)
  and download their files. Admins also get the live console, file management, per-game settings and user management.
  *Organizers* run sign-up rounds and the games they add or generate the way admins do, without administering the
  server. New people join with an *invite link*, and someone who forgot their password gets a link to set a new one.
- **Encrypts game connections.** With a certificate (such as the one your reverse proxy already has), every game
  server speaks `wss://`; Archipelago's clients switch over by themselves, and renewed certificates are taken over.
- **Spectators and stream overlays.** A game can have a link that shows it without signing in: everyone's progress,
  the standings and the race clock, optionally what happens in it (never the chat). It comes with an overlay for OBS.
- **Reminds players who stopped playing.** A player who has not played for some days while the others play on is
  reminded on their dashboard and on Discord, with how many hinted items of others wait in their world and the offer
  to hand the slot over.
- **A text client in the browser.** Players chat with the multiworld and use the server's commands (`!hint`,
  `!remaining`, …) on the game page, without Archipelago's own text client.
- **Progress over time and an archive.** A chart shows how far each player got and when; games that are over move to
  an archive page, finished (with the playing time and who reached their goal when) or aborted.
- **Small things that save time.** *Open in Archipelago* starts a player's client already connected to their slot;
  hints are marked priority or avoid from the browser; planned starts and sign-up deadlines go into the calendar; a
  game page in a background tab counts what is new in its title; lists can be searched; light or dark as you like.

## Quick start

Images are published for `linux/amd64` and `linux/arm64` (Raspberry Pi 4 and 5 with a 64-bit system, ARM NAS, Apple
silicon); Docker picks the right one.

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
   - **Public address**: the name or IP your players type (see [Reaching your games](docs/configuration.md#reaching-your-games)).
4. Open the **WebUI** from the container's menu and create the administrator account. For safety this asks for a setup
   code: the container's **Logs** (in the container's menu) show it, and it is in `setup-code.txt` in the appdata
   folder. So nobody else who reaches the web UI before you can make themselves its administrator.

The image runs as Unraid's `nobody:users` (99:100) by default, so the appdata folder stays accessible from your shares.

### Docker Compose

```sh
git clone https://github.com/NiklaskoDev/ArchipelagoManagerDocker.git
cd ArchipelagoManagerDocker
docker compose up -d        # uses the published image; set PUID/PGID/PUBLIC_HOST in docker-compose.yml first
```

Open <http://localhost:8080> and create the administrator account with the setup code from
`docker compose logs archipelago-manager` (or `./config/setup-code.txt`). To build the image yourself instead, uncomment
`build: .` in `docker-compose.yml`.

Either way, players connect to the address you give as *Public address* (`PUBLIC_HOST`): see
[Reaching your games](docs/configuration.md#reaching-your-games). With a certificate, such as your reverse proxy's, game
connections can be [encrypted](docs/configuration.md#encrypted-game-connections).

## Documentation

- **[Configuration](docs/configuration.md)**: environment variables, the address players connect to, and encrypted
  game connections.
- **[Playing](docs/players.md)**: configs, games and slots, the tracker, the text client, language and theme, API
  tokens.
- **[Running the server](docs/admins.md)**: for administrators and organizers: generating, hosting and running games,
  sign-up rounds, users and settings.
- **[Custom games and base ROMs](docs/custom-games.md)**: adding APWorlds, also from GitHub releases, and the ROMs some
  games need.
- **[Data, updates and troubleshooting](docs/maintenance.md)**: what is kept where and how to back it up, updating, and
  what to do when something goes wrong.
- **[Security notes](docs/security.md)**: what the manager protects, and what is up to you.
- **[Development](docs/development.md)**: running it from source, the tests, and how the code is organized.

## Limitations

- On `linux/arm64`, a further Archipelago version installed from the web UI comes without The Wind Waker: its
  requirement `dolphin-memory-engine` has no build for arm64 and the image has no compiler to make one, so that world
  is skipped (the install log says so). The version the image comes with has it.
