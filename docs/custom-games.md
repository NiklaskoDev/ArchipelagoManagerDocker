# Custom games and base ROMs

## Custom games (APWorlds)

Games that are not part of Archipelago itself come as `.apworld` files.

- An admin uploads one under **Custom games**. It is installed immediately and available for generating, and players
  can create configs for it in the [options form](players.md#the-options-form) with the world's own options.
- When a config or an uploaded seed needs a game that is not installed, a prompt says which one and offers to add it.
  *Hosting a seed does not need the APWorld* (the seed carries what the server needs), but players need it to patch and
  play: the list of all games at the top of the page links each installed custom game's file.
- Users can *submit* an APWorld; it waits for an admin to approve it (switch this off under Admin → Settings). Below
  the list of all games, players see what they submitted themselves (files and GitHub repositories), administrators
  every custom game.
- Files placed by hand in Archipelago's worlds folder are adopted automatically at startup.
- Some APWorlds are released without the `archipelago.json` manifest that Archipelago's *Build APWorlds* puts in (A
  Dance of Fire and Ice 1.3.1, for one). Archipelago 0.6 still loads them, so they can be added like any other: the
  game's name is read from the world's code, which is only read for this, not run. Archipelago 0.7.0 refuses such a
  file, and with it every other custom game, so it is not added to installed versions from 0.7.0 on. If the name
  cannot be read (the code does not spell it out as `game = "…"` on its World class), the file is refused; ask its
  author for a version with a manifest.
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

## Games that need a base ROM

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
