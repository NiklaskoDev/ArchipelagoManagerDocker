# Security notes

- Passwords are stored as salted scrypt hashes; sessions are `HttpOnly`, `SameSite=Lax` cookies, also `Secure` when the
  browser talks https with the server (judged by the request, or the page it comes from behind a proxy that does not
  say so, never by the configured address alone); state-changing API
  calls require a custom header, which blocks cross-site requests; failed logins are rate limited per address (an IPv6
  address counts with its whole /64 network, which one connection usually has, so hopping between them buys no
  guesses).
- Invite and reset links carry 256-bit random tokens of which only a SHA-256 hash is stored, like API tokens. The
  token sits in the fragment of the link (`#/invite/…`), which browsers never send to a server, and the page posts it
  in the body of a request, so it appears in no address and no server log. Wrong tokens count as failed logins of the
  address. An invite never makes an administrator, and API tokens can neither make invites or reset links nor use
  them. Joining with an invite and setting a password with a link are in the administrators' log.
- API tokens are 256-bit random values of which only a SHA-256 hash is stored. They go in the `Authorization` header,
  which another website cannot make a browser send, so they need no CSRF header. A request with a token is judged by
  the token alone, never by a cookie it also carries, and a token cannot manage tokens, passwords, sign-ins, Discord
  links or user accounts, so a leaked token cannot lock its owner out, make an administrator of its own or keep itself
  alive after it is revoked. Other `Authorization`
  headers, such as a reverse proxy's basic auth, are ignored.
- Archipelago's game connections are plain websockets unless a certificate is set (see
  [Encrypted game connections](configuration.md#encrypted-game-connections)); without one, a room password only keeps people out and
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
