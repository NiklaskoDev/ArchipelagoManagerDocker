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
  edited directly too, or started from the documented template. Players can share a config, and the others copy it
  into their own library with their own player name.
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
  server.
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
   - **Public address**: the name or IP your players type (see [Reaching your games](#reaching-your-games)).
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

### Encrypted game connections

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

## Using it

### As a player

1. **My configs** → upload your YAML, or *New config* and pick your game: its options open as a form (see
   [The options form](#the-options-form)). The editor checks the file when you save and tells you what is wrong. The
   player name is the slot name in the game. The search above the list finds a config by its file, player name or game.
2. When an admin generates a game with your config, or uploads a game with a slot of that name, it appears under
   **Games** with the server address, password, your slot name and your files. If the game allows it you can *claim*
   a slot yourself ("This is mine"). The search above the list finds a game by its name, its worlds or your slot name.
3. Download your patch file(s), patch your game, and connect with the Archipelago client of your game. If the game is
   *Asleep*, opening its page wakes it up: connect once it shows *Running*. When there was no port free for it while
   it slept, it may come back on a different port, so take the address from the page.
   - *Open in Archipelago* next to your slot name does the connecting for you, if Archipelago is installed on your
     computer: its Launcher opens the client of your game, with the address, your slot name and the password filled
     in. Not every game's client takes such a link (`archipelago://`); for the others the Launcher opens the Text
     Client, connected to your slot.
4. While you play, **Your tracker** on the game page shows the items you received (and from whom), the locations you
   still have to check, and your hints: where your items are, and which items of other players are in your world. It
   is read from the server's save, so it can be up to a minute behind.
   - It also shows your hint points, what a hint costs, and how many hints they buy (or how many checks until the
     next one).
   - **Mark your hints**, as in Archipelago's own client: next to each hint for one of your items, choose *Priority*
     (you need it), *No priority*, or *Avoid* (you can do without it, a trap say). Whoever has the item in their world
     sees it in their client and in their tracker, which lists the items the others need most first. It needs the
     server running; the manager sends it in your name, like the text client below.
   - **Release my items** (`!release`: everything still in your world goes to its players) and **Collect my items**
     (`!collect`: everything of yours still in other worlds comes to you) appear when the game's rules let you use
     them, for example only after your goal. Otherwise the tracker says when they become available, or that they
     happen by themselves at your goal. Both ask before they act, as neither can be undone. The manager sends the
     command in your name, like the text client below, and shows the server's answer.
5. The **Text client** on the game page does what Archipelago's text client does, in the browser: *Connect*, then chat
   with everyone in the multiworld or type the server's commands, such as `!hint Hookshot` (a hint for an item; the
   names of your items are suggested as you type), `!hint_location`, `!remaining` or `!help`. It shows what happens in
   the multiworld (items sent and found, hints, chat) in Archipelago's colors; *Only messages about me* hides the rest.
   - The manager connects to the server for you, as your slot with Archipelago's `TextOnly` tag: the other players
     see you as "viewing", and it neither receives items nor checks locations, so it does not get in the way of your
     game's own client.
   - The connection stays open while the page is open (also across a reload, with the messages so far) and closes
     about a minute and a half after you leave the page, or right away with *Disconnect*.
   - Commands that start with `/` belong to Archipelago's own clients and are not available here; the tracker above
     shows what `/received` and `/missing` would.
6. **Activity** on the game page follows what happens in the multiworld, for every player in the game: chat, items
   sent and found, hints, who joins and leaves, goals, releases and collects, and the countdown. It is read from the
   server's log as it runs and kept with the game, so it is there for players who were not connected, with the time
   of each line. Filter it by chat, items, hints, or joins and goals, or show only what concerns your own slots. The
   server's own output, the console and remote administration (`!admin`) are not shown.
   - While the game page is open in a background tab, its title counts what is new for you, such as
     *(3) Friday Async*: what the others say in the chat, the items they send you, and hints for their items in your
     world. Back on the tab, the count goes.
7. **While you were away:** come back to a game page after more than ten minutes and a card at the top sums up what
   happened since your last visit: the items others sent you (and from whom), open hints between you and the others
   (your items in their worlds, their items waiting in yours), who got stuck, who reached their goal or released their
   items, and how many chat messages came in. It stays, also after a reload, until you press *Got it*. The page counts
   as visited while it is open and in view; a tab in the background counts as away.
8. **Your status:** *Set status* under your slot in the slot table tells the others how you are doing: *Playing*,
   *Stuck* (waiting for items, "BK"), or *On a break*, with a short note such as "need the Hookshot". Everyone in the
   game sees it in the slot table and in the activity, and the Discord channel hears when someone is stuck, with where
   the items hinted for them are, so whoever holds one knows (one message per player in ten minutes). Once you reach
   your goal it is no longer shown; giving up the slot clears it.
9. **Handing your slot over:** no time to finish? *Hand over* next to your name in the slot table offers your slot to
   a user you name, or to whoever takes it (everyone who can see the game: its players, or every user if the game is
   visible to all). Whoever *takes it over* becomes the slot's player with everything found so far: its files, its YAML
   (copied into their configs) and its progress, and plays on with the same slot name. Until then the slot stays
   yours, and *Take back* ends the offer. A user it is offered to sees the game on their dashboard (*Slots looking for
   a player*) even if they are not in it yet, and can say *No, thanks*. With *only one slot each*, someone who already
   has a slot cannot take another one. The activity and the Discord channel hear about the offer and the takeover.
10. **Reminders:** a game can remind its players when they have not played for a few days (three by default, set by
    the admin and per game) while the others play on. A card on the dashboard (*Games waiting for you*) and on the game
    page says so, with *Set status* and *Hand over*, and the Discord channel posts a reminder, which mentions you if you
    ticked *You have not played a game for a while* under *Discord mentions…*; it says how many hinted items of the
    others are waiting in your world. It comes again after as many days, three times at most. Playing on, setting a
    status or taking over the slot counts as playing; on *a break* you are not reminded, nor once you reached your
    goal or offer your slot.
11. **Progress over time** on the game page charts every player's share of their locations checked, from the first
    check on, with a dot where someone reached their goal. Point at the chart (or focus it and use the arrow keys) for
    everyone's numbers at that moment; pressing a name in the legend brings that player to the front. With more than
    eight players, your own slots are in color and the others gray. The table below lists the standings: who reached
    their goal first, and after how long, then who got furthest.
12. **Archive:** *Games → Archive* lists the games you were in that are over, *Finished* (every player reached their
    goal) or *Aborted* (moved there before that), with when they ended, the playing time, how many reached their goal,
    and a link to the results. Filter by outcome or search by game, world or slot name.

**Generating your own games** (when an admin lets players generate, *Admin → Settings*): *Generate* works as it does
for admins, with your own configs. So that one player cannot fill the server, a game you
generate has at most 15 worlds, you can have 3 unfinished games of your own at a time (generations still under way
count), and one of them running (a game asleep until its players come back counts as running). *Generate* shows how
many you have left. When a game of yours will not be played to the end, *Move to the archive* on its page lists it as
aborted and frees its place; *Back to the running games* takes it out again while you have a place free. A game you
generate with *start the server when done* while another of yours runs is generated but not started. Organizers and
administrators have none of these limits, and they can start a game of yours while another one runs.

**Sharing configs:** *Share* next to one of your configs offers it to everyone on the server; *Stop sharing* takes it
back. Configs others share are listed under **Shared by players** on **My configs** (also reachable from *New config*):
look at one, download it, or *Copy* it into your own configs. The copy asks for your player name and puts it into the
file in place of the original one (everything else, comments included, stays as it is); a file with several players is
copied as it is, so change the names in the editor. The copy is yours: neither the copy nor the original changes when
the other does. Administrators can stop sharing any config.

**Rolling a config:** a config can leave things to chance: weights over several values (`full: 50, minimal: 50`),
random ranges, triggers, even the game. *Roll* in the config editor rolls it once the way the generator does, with
Archipelago's own code, and shows the file that comes out: a game and one value per option. *Roll again* for another
outcome, *Save as a new config* to play exactly that one. (The config itself keeps its weights: every generation rolls
them anew.)

**Sign-ups:** open rounds are on the dashboard and under **Sign-ups**. *Join* → pick one of your configs or upload a
YAML. It is checked against the round's rules right away (allowed games, unique player names, how many slots each
player may enter). A file with several players, its documents separated by `---`, takes a slot for each of them, so it
cannot bring more games into a round of one per player. Your entry is a copy: if you edit the config afterwards, the
round page offers to use the new version. You can withdraw until the round closes. *Add to calendar* on a round with a
deadline puts the deadline into your calendar, with a reminder a day before (an hour, if it is closer than that).

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

Most of this is also open to **organizers** (*Admin → Users*, role *Organizer*), for the games and sign-up rounds they
add, generate or open: they upload and generate games (from their own configs, even when players may not generate),
open sign-up rounds, and run those like an admin, with settings, slots, files, console, backups, spectator link and
deleting. They cannot see or change other people's games and rounds (unless a game is visible to everyone, and then
only as a player sees it), nor users, server settings, `host.yaml`, custom games, ROMs or Archipelago versions. Their
games may post to a Discord webhook (`https://discord.com/api/webhooks/…`) and use the ports in `PORT_RANGE`. What
they do is in the admins' log. A game generated by a sign-up round belongs to whoever opened the round.

- **Add a pregenerated game:** *Games → Add a game*, drop the zip. The slots are matched to users whose config has the
  same player name (case-insensitive) and game. If the zip contains the players' YAMLs, a user whose config is one of
  them wins over others with the same name. If it is still ambiguous the slot is left unassigned rather than guessed;
  assign it from the slot's *Owner* drop-down (which also ends an offer to hand the slot over; you can take one back
  under the drop-down too). Turning on *claimable* in the game's settings lets players pick their own;
  add *only one slot each* so nobody can take more than one (you can still assign extra slots by hand).
- **Generate a game:** *Generate*, tick the configs, set the options, *Generate*. Slot owners are taken from the configs.
  If a generation fails, the job page marks the configs the generator complained about and says what is wrong with them.
- **Generate again:** *Generate again* on a game (or a finished job, or the arrow in *Recent jobs*) opens *Generate* with
  the same configs and options filled in, to roll a new seed or to retry after fixing a config. The configs are used as
  they are now; one that was deleted since is left out, and the page says which. The button is there as long as the
  generation job is kept (the latest 50).
- **Templates of generation options:** *Save as template* under the options on *Generate* (or in a sign-up round's
  form) keeps what the form holds under a name, such as *Race* or *Async*: spoiler log, race mode, progression
  balancing, plando, whether the server starts afterwards, the `meta.yaml` and the Archipelago version. *Fill in from a
  template…* puts it back into the form; the players, the name and the seed are never part of it. Organizers and
  administrators can also give the new game its own server settings (*Settings for the new game*: hint cost,
  `!release`, sleeping, …) instead of the defaults, and keep those in the template too, and they can share a template
  with everyone who generates. Each template is changed (*Update*) and deleted only by whoever saved it, or by an
  administrator. A sign-up round has no `meta.yaml` or version of its own, so it leaves those of a template out.
- **Run a sign-up round:** *Sign-ups → New round*: a name, a deadline (or none, to close it by hand), optionally the
  allowed games and how many slots each player may enter (a file with several players takes one for each of them),
  and the generation options. At the deadline the sign-ups close and the game is generated and started; switch that
  off to generate by hand. If generating fails (for example a game is not installed), the round stays closed with the
  error: fix the entries, then *Generate* again or *Reopen*.
- **Mystery rounds:** tick *Mystery round* when opening a round and give it a file of weights: one player with
  weights over games and options, as for a mystery yaml. *Pick the games* writes it for you: add games from the
  installed ones, give each a weight (3 and 1: three times as often) and choose *Its default options* or *Every option at
  random* (every option that can be left to chance, except deathlink, progression balancing and accessibility), then
  *Write the weights*. The file stays editable, to fix single options by hand; you can also paste or load your own. It is checked like a config, and rolled once to make sure
  the generator can take it; games it may pick that are not installed are named. Players then join with nothing but a
  player name: their entry is the weights with that name, so the generator rolls a game and options for each of them,
  and nobody knows what they play before the game is generated. Everyone can download the weights (what one may get is
  no secret), and the round lists the games in them. Changing the weights changes every entry. A round becomes a mystery
  round, or stops being one, only while nobody has entered.
- **Test a round before the deadline:** *Test generation* on the round generates the current entries with the round's
  options and throws the result away; the round stays open. The page shows whether it passed and says when entries or
  options have changed since. When it fails because of an entry, that entry is marked with the generator's error, for the
  admin and for the player who entered it (who can fix the config and *Use the new version*), so the real generation at
  the deadline does not fail on it. A passed test is a good sign, not a promise: the real generation rolls a new seed.
- **Discord notifications:** *Admin → Settings → Discord notifications*. In Discord, open the channel's settings →
  *Integrations* → *Webhooks* → *New Webhook* → *Copy Webhook URL*, paste it, and *Send a test message*. Set *Address of
  this web UI* (under *General*) so messages link to the right page, pick the language of the messages (English,
  German, French or Dutch), and untick the kinds of messages you do not want. Goals and found hinted items are read from
  the server's save, so they are posted up to a minute after they happen. Many hinted items
  found at once (after a `!release` or `!collect`) come as one message.

  Players who linked their Discord account (see below) can have these messages mention them, so Discord notifies them:
  in the user menu, *Discord mentions…*, they tick what they want to hear about: a new game they play in is ready,
  a game they play in is online, someone found an item hinted for them, someone in a game they play in is stuck,
  a slot is offered to them (or, in a game they can see, to whoever takes it), sign-ups open, or sign-ups close soon and they have not entered yet. Nobody is mentioned unless they asked for it,
  and only in messages you have switched on; untick *Let players be mentioned* to keep every message silent.

  A game can post to a channel of its own: paste another webhook under *Settings → Discord* on the game page (and
  *Send a test message*). Its messages (online, crashes, goals, stuck players, found hinted items) then go there
  instead of the main channel, which is not needed for them; the kinds of messages are still the ones ticked here. A
  webhook that fails is reported on the game page.
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
  A linked account shows its Discord picture in the top bar instead of the outline of a person. The browser loads it
  from Discord's image server (`cdn.discordapp.com`, without telling it which page asks); each sign-in with Discord
  brings the current picture, so accounts linked before this version get theirs with their next one.
- **Archipelago versions:** *Admin → Settings → Archipelago versions*, type a release number (such as `0.6.2`) and
  *Install*. The manager downloads that release's source from GitHub, makes a Python environment for it with
  Archipelago's requirements and those of its worlds (as the image does for its own version), and checks that it
  loads; the log shows each step. It takes a few minutes and some hundred megabytes in the data folder
  (`ap-versions/`), and needs internet access while it runs. Worlds whose requirements need a C compiler do not load in
  a further version (the image has none); the card lists them. Then pick the version:
  - for a game: *Settings → Archipelago version* on the game page (the server needs a restart to switch). The *Connect*
    box tells players which version the game runs on, so they use a client of it. Tracker, progress, text client and
    Discord messages read the game with its own version;
  - for a generation: *Archipelago version* on *Generate* (also offered to players who may generate); the game it makes
    runs on the same version;
  - for an upload: *Archipelago version* in *Add a game*.

  Custom games (APWorlds) are added to every installed version they say they work with (`minimum_ap_version` and
  `maximum_ap_version` in their manifest), and the base ROMs of the image's version are shared with the others. Each
  further version has its own `host.yaml` in its folder. A version can be removed once no game uses it. Sign-up rounds
  always generate with the image's version.
- **Run a game:** *Start / Stop / Restart* on the game page. *Settings* holds the room password, the remote-admin
  password, hint cost, `!release`/`!collect`/`!remaining` rules and when the game falls asleep. Passwords and rules apply
  to a running server immediately; the port and a few startup options need a restart (the UI tells you).
- **Sleeping games:** *Falls asleep after (minutes)* in a game's settings (and in the defaults for new games under
  *Admin → Settings*). Tick *Never falls asleep* to keep a game running until it is stopped; that is the default.
  - A game falls asleep when nobody was connected to its server for that many minutes: the manager stops the server
    and the game shows *Asleep*. A connected client keeps it awake, however long its player takes, and so does the
    text client on the game page.
  - It wakes up when one of its players opens the game page, whether or not players may start games themselves. A
    look at the page of a game that is awake keeps it awake for that long again. A game opened ahead of its planned
    start waits for it awake, and a finished game is not woken up by a visit (its players can press *Wake up*).
  - Admins start an asleep game with *Start*. *Stop* turns it into a stopped game, which a visit does not wake up.
  - An asleep game stays asleep through a container restart and keeps its port. Only when a game is started and no
    port in `PORT_RANGE` is free does it take the port of a game whose server is not running: a finished one first,
    then a stopped one, then an asleep one, each the one that stopped longest ago. That game gets a free port when it
    starts again; the game page always shows the current address.
- **Planned start and races:** *Settings → Planned start* on the game page: a start time, how many minutes before it
  the server opens (10 by default, so everyone can connect and get ready), and whether a countdown runs.
  - Setting it posts a *Planned start* message on Discord, mentioning the players who asked to hear when their game goes
    online. The game page and the game list show the start with a clock counting down, and *Add to calendar* next to
    it gives players a calendar file with a reminder when the server opens.
  - Until the server opens, players cannot start it themselves; admins can, and the planned opening then leaves it
    running.
  - At the opening time the server starts by itself and is announced like any start. With the countdown on, the
    server runs Archipelago's `/countdown` so that every client counts down and says GO at the start time.
  - That moment is the start of the race. Goals are taken from the server's log the moment a client reports them, to
    the second, and the results table and the Discord goal messages show each player's race time (`1:23:45`).
- **Console:** every server's log, plus a command line for Archipelago's server commands (`/send`, `/hint`, `/help`, …).
- **Progress:** per slot checks and whether a player is online, read from the server's save (refreshed about once a
  minute while the game is running). The tracker on the game page lets you pick any slot and see its items, locations
  and hints.
- **History and archive:** every look at the save is recorded: a point whenever a slot's checks change (timed by the
  save itself, which knows when a slot last checked a location), when each slot first shows its goal, and when the first
  location of the game was checked. Once every player has reached their goal, the game is *finished*: it gets a
  *Finished* badge, its chart becomes the *Results*, and the *Archive* page (a tab of *Games*) lists it with the playing
  time (from the first check until everyone was done) and how many reached their goal. The Discord message that everyone finished says how long it took.
  - *Move to the archive* (game page; admins, the game's organizer, and the player who generated it) ends a game that
    will not be played to the end: the archive lists it as *Aborted*. *Back to the running games* undoes it. Nothing
    about the game's progress changes either way, and a running server keeps running until it is stopped.
  - *Share the spoiler log with the players when everyone has finished* (game settings) makes the spoiler log visible
    to every player in the game at that moment.
  - *Reset progress* starts the history over. Games that already had progress before the manager kept a history get a
    first point where they stood at the next start of the manager, and finished ones go to the archive; their times
    before that are unknown (shown as "before recording began").
- **Backups of a game's progress:** *Backups* on the game page. While the save changes, a copy is kept every 30 minutes
  (the latest 48, about a day of play), plus one before every *Reset progress* and every restore, and whenever you
  press *Back up now* (the latest 20 of those). *Restore* puts a copy back while the server is stopped, after keeping
  the current progress as a copy, so a reset or a restore can always be undone.
- **Spectators and stream overlays:** *Spectators → Make a link* on the game page gives a link anyone can open
  without signing in. It shows the game's state, every player's progress, the standings with their race times, the
  chart and the countdown or race clock; *Also show what happens* adds the activity feed without the chat (hints in it
  tell viewers where items are). It never shows the address, the passwords, files or user names, only slot names.
  *Overlay for OBS* is the same link with `/overlay` at the end: a compact, transparent panel to add as a browser source
  (about 400 px wide). *New link* ends the old link, *Turn off* ends it altogether.
- **Reminders for players who stopped playing:** *Remind players who stopped playing after (days)* under *Admin →
  Settings → Defaults for new games* (3, or 0 for never), and per game in its *Settings*. A player is reminded once
  someone else in the game has played after they stopped (a game everybody left alone stays quiet), and again after as
  many days each time, three times at most. Tick *A player has not played for a while* under the
  Discord notifications for the channel message.
- **Moving a game from archipelago.gg:** archipelago.gg never hands out a room's multiworld or its save, so add the
  game here from the zip the generator made (the one uploaded there, or only its `.archipelago` file), then *Move from
  archipelago.gg* on the game page: paste the room's address (`https://archipelago.gg/room/…`) and *Take over the
  progress* while the server is stopped. The manager reads the room's public tracker: the locations every slot
  checked, the items it received (in order), the hints, aliases, who reached their goal and when everyone last played.
  It refuses a room that is not this very multiworld (other slots, games or numbers of locations, or items and hints
  that lie elsewhere, as in another seed of the same players). The game's current progress is kept as a backup first,
  and patch files the game has none of yet are downloaded from the room. Not carried over: hint points already spent
  (everyone can ask for hints as if they had spent none) and what some games keep in the server's data storage. The
  tracker is up to a minute behind the room, so let everyone stop playing there first. The server needs to reach
  archipelago.gg for this.
- **Users and settings:** *Admin* has users and their roles (user, organizer, administrator), registration, what
  players may do, defaults for new games, Archipelago's `host.yaml`, and system information.
- **Log:** *Admin → Log* lists who changed what: every change an administrator or organizer makes (games, slots, users, settings,
  custom games, generation, sign-up rounds, configs, ROMs) and every start, stop, restart and wake-up of a game,
  players' included, with what it was done to and what changed. Names are kept as they were, so a deleted game or user is
  still named. Filter by person or area; the latest 50,000 entries are kept. Values of passwords and secrets are never
  written: the log says *which* settings changed, and console commands are masked like the console.
- **Backup of everything:** *Admin → Settings → Backup of everything → Download a backup* makes one zip of the whole
  data folder (see [Data and backups](#data-and-backups)) and downloads it.

### API tokens

Scripts and bots (a Discord bot, a status page, a scheduled script) use the same JSON API as the web UI, with a personal
token instead of signing in. *API tokens* in the user menu makes one: a name, the access, and how long it lasts (30 or
90 days, a year, or never). The token is shown once; only a hash of it is stored.

- **Read only** tokens can make `GET` requests only: look up games, slots, progress, the activity feed.
- **Read and change** tokens can do everything their user can do in the web UI, such as starting a game or claiming a
  slot. An administrator's token can do what administrators can.

Send it in the `Authorization` header:

```sh
curl -H "Authorization: Bearer apm_…" https://archipelago.example.com/api/games
curl -X POST -H "Authorization: Bearer apm_…" https://archipelago.example.com/api/games/1/start
curl -H "Authorization: Bearer apm_…" "https://archipelago.example.com/api/games/1/activity?after=0"
```

Tokens cannot make or list tokens, change passwords, sign in or out, link Discord, or add, change or remove user
accounts (an administrator's token can list them). The token page shows when each
token was last used, and from where; *Revoke* ends one at once. Administrators see everyone's tokens there and can
revoke any of them. A token also stops working when it expires or its user is disabled, and goes with its user. A new
password ends a user's tokens as well, in case someone who knew the old one made some: when an administrator sets it,
and when you change your own (unless you untick *Also revoke my API tokens*). The administrators' log names the token
an administrator's change was made with.

### Language

The web UI is available in English, German (Deutsch), French (Français) and Dutch (Nederlands). It follows the
browser's preferred languages at first; switch it with the language button on the sign-in page or under *Language* in
the user menu (the choice is remembered in that browser). Dates and numbers follow the language, in the browser's own
variant of it where there is one (Swiss German, Canadian French, Belgian Dutch, …). Calendar files come in it too.

It is dark, or light when the system asks for that. *Theme* in the user menu picks *Light* or *Dark* instead, for that
browser (its other tabs follow at once); *As on this device* goes back to the system's choice.

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

Some games need their original ROM on the server to *generate*. With Archipelago 0.6.8 these are A Link to the Past,
Lufia II Ancient Cave, MegaMan Battle Network 3, Ocarina of Time, Secret of Evermore, Super Mario World, The Legend of
Zelda and Yoshi's Island, plus custom games that ask for one. Other ROM games only need the ROM on the players'
computers to patch. Hosting a pregenerated seed never needs a ROM, and players always need their own to patch.

**Admin → Settings → Base ROMs** lists every ROM the installed games ask for. The ones generating needs come first; the
ones only players need to patch are folded away below.

- *Upload* checks the file against the checksums the game knows (its region and revision). This is the same check
  Archipelago runs when you pick the file in its own settings. The file is then stored where the game looks for it,
  under the name it expects; the name of the uploaded file does not matter.
- A wrong dump, another region or revision, or a different game is refused and not kept.
- *Replace* swaps a ROM, and the bin icon removes one.
- When a ROM is missing, the generate page, the config and the sign-up round say so, and administrators get an
  *Upload the ROM* button right there. Generating is refused right away instead of failing in the queue, or at a
  round's deadline.

Copying a ROM yourself works too: put it into `ap-data/Archipelago/` in the data folder (on Unraid
`/mnt/user/appdata/archipelago-manager/ap-data/Archipelago/`) under exactly the name shown. Or point the game's entry in
`host.yaml` (Admin → Settings) at it. The warnings disappear as soon as the file is there. The manager only writes
inside the data folder, so if `host.yaml` points a game somewhere else, put the file there yourself.

Whether generating needs a game's ROM is worked out by running the game's own check before generating with the file
missing. That runs in memory only; `host.yaml` is not changed.

## Configuration

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

## Security notes

- Passwords are stored as salted scrypt hashes; sessions are `HttpOnly`, `SameSite=Lax` cookies; state-changing API
  calls require a custom header, which blocks cross-site requests; failed logins are rate limited per address (an IPv6
  address counts with its whole /64 network, which one connection usually has, so hopping between them buys no
  guesses).
- API tokens are 256-bit random values of which only a SHA-256 hash is stored. They go in the `Authorization` header,
  which another website cannot make a browser send, so they need no CSRF header. A request with a token is judged by
  the token alone, never by a cookie it also carries, and a token cannot manage tokens, passwords, sign-ins, Discord
  links or user accounts, so a leaked token cannot lock its owner out, make an administrator of its own or keep itself
  alive after it is revoked. Other `Authorization`
  headers, such as a reverse proxy's basic auth, are ignored.
- Archipelago's game connections are plain websockets unless a certificate is set (see
  [Encrypted game connections](#encrypted-game-connections)); without one, a room password only keeps people out and
  is sent unencrypted. Do not reuse a password you care about. Games set to run without encryption stay unencrypted.
- The web UI speaks plain HTTP. Do not expose it to the internet without an HTTPS reverse proxy.
- Spoiler logs, bundled YAMLs and other players' files are visible to admins (and the game's organizer) only, unless
  they share them.
- The console is for admins and the game's organizer only, and runs real server commands. Remote admin (`!admin`) is off
  unless you set a password.
- A spectator link is a random 144-bit value; whoever has it sees the game's progress, never anything to connect with
  or a user's name. Make a new link to end an old one.
- Only admins upload base ROMs. A ROM is checked against the game's own checksums before it is kept, and is written only
  inside the data folder (or Archipelago's folder for user files), never where `host.yaml` might point elsewhere.
- The text client speaks in a slot's name, so only the player who owns the slot can use it, admins included. The
  manager connects to the server inside the container, so the browser only talks to the web UI (through your HTTPS
  proxy, if you use one), never to the game's unencrypted port. Opening the connection is a state-changing request
  like any other, so another website cannot open it in a player's name.
- Discord messages never contain passwords and mention nobody but the players who asked for it, by their Discord
  account ID (`@everyone`, roles, and mentions in a player name stay plain text).
  Treat the webhook address like a password: anyone who has it can post to the channel. Only admins see and set the
  webhooks, the games' own included; an organizer can give their own games a Discord webhook, and nothing but one.
- Configs are private to their owner (and admins) until the owner shares one. Others can then read and copy it, never
  change or delete it; a disabled user's configs are no longer offered.
- Signing in with Discord uses OAuth2 with a single-use `state` bound to the browser that started it, a fixed redirect
  address, and Discord's account ID; Discord's tokens are revoked right after use. Disabled users cannot sign in this
  way either.
- Server logs never show the room or admin passwords (MultiServer repeats them when they are set), and neither does a
  game server's command line, which every process on the machine can read: the passwords reach the server through its
  console.
- Without a session, requests may be only a few kilobytes; only admins and organizers (and, for custom games, players
  allowed to submit them, up to 100 MB) can upload large files. Players can keep at most 200 configs and have at most 5 custom
  games and 5 GitHub repositories waiting for approval. Each player rolls one file at a time, and rolls never take more
  than half of the places for Archipelago's helper processes, so rolling in a loop holds up nothing else.
- An approved GitHub repository installs exactly the release file the admin was shown; if the repository changed it in
  the meantime, the admin is asked to look again. Downloads are checked against GitHub's SHA-256 digest.
- Creating the first administrator takes a random setup code that is only in the container's log and in
  `setup-code.txt` in the data folder (readable by the manager's user only, and deleted once it is used). Whoever reaches
  a fresh install before you, directly or through DNS rebinding, cannot take it over: the address a request names is
  the sender's to choose, the code is not.
- Player files may use anchors and aliases (`&name`, `*name`), but not so that a small file stands for a huge one once
  written out ("billion laughs"): such files are refused wherever they come in.
- Usernames are letters and digits of any language plus spaces and `. _ -`, with at least one letter or digit, and
  from one alphabet only (Japanese, Korean and Chinese names use their scripts together, as usual), so that no name can
  pass for another one: `аdmin` with a Cyrillic `а` is refused. Names in player files may be anything Archipelago
  takes; one that changes the direction of the text cannot turn the text around it on the page or in Discord.

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
- **Image smoke test:** `SETUP_CODE=… python tests/smoke.py http://localhost:8080` against a fresh instance, with the
  setup code from its log. CI runs it against the built image before publishing.
- The front end is plain ES modules in `manager/archipelago_manager/static/`, with no build step.
- `ap_helper.py` runs inside Archipelago's own Python environment: it lists worlds, decodes multidata and saves,
  renders templates and describes each game's options for the options form, so the manager never imports Archipelago's
  code itself. `ap_server.py` starts a game's `MultiServer.py` there, with the passwords it reads from the server's
  console. `yamlform.py` turns a YAML into the form's values and back.

## Limitations

- On `linux/arm64`, a further Archipelago version installed from the web UI comes without The Wind Waker: its
  requirement `dolphin-memory-engine` has no build for arm64 and the image has no compiler to make one, so that world
  is skipped (the install log says so). The version the image comes with has it.
