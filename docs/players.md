# Playing

What players do in the web UI: their configs, their games and slots, the tracker, the text client, and more.

1. **My configs** → upload your YAML, or *New config* and pick your game: its options open as a form (see
   [The options form](#the-options-form)). The editor checks the file when you save and tells you what is wrong. The
   player name is the slot name in the game. The search above the list finds a config by its file, player name or game.
   - *Check* next to a config asks Archipelago whether the generator would take it, without generating anything, and
     lists what it finds in Archipelago's own words: option values it does not take (with what it does), item and
     location names it does not know (with the name it guesses you meant), games that are not installed, and, as
     warnings, option names it would silently ignore, such as a typo. Unlike a generation, it does not stop at the
     first problem, and it tries every value and every game the file gives a weight to, not just one roll. In the
     editor, *Check* takes the file as it is there, saved or not. What only shows when a seed is made, such as items
     that cannot be placed with these options, is beyond it.
2. When an admin generates a game with your config, or uploads a game with a slot of that name, it appears under
   **Games** with the server address, password, your slot name and your files. If the game allows it you can *claim*
   a slot yourself ("This is mine"). The search above the list finds a game by its name, its worlds or your slot name.
   - A game's page has three tabs. **Play**: *Connect* (the address, the password, and your slot with its files and
     what you do with it), *Your tracker*, and *Chat and activity*. **Players**: everyone's slots, with their owner,
     progress and status, and *Progress over time*. **Manage**, for whoever runs the game. The address keeps the tab,
     so a link or a reload opens the same one.
3. Download your patch file(s), patch your game, and connect with the Archipelago client of your game. If the game is
   *Asleep*, opening its page wakes it up: connect once it shows *Running*. When there was no port free for it while
   it slept, it may come back on a different port, so take the address from the page.
   - *Open in Archipelago* next to your slot name does the connecting for you, if Archipelago is installed on your
     computer: its Launcher opens the client of your game, with the address, your slot name and the password filled
     in. A game without a password gets `None` in the link, as on archipelago.gg. Only a few games' clients take such
     a link (`archipelago://`, for instance TUNIC, The Messenger and Starcraft 2): the Launcher then lets you pick
     between that client and the Text Client. For all other games, such as those played through BizHawk or SNI, it
     opens the Text Client, connected to your slot, and you start the game's own client yourself. This is the
     Launcher's rule, the same for links from archipelago.gg. The button is on your game's card on the dashboard, in
     the list of games and on the game page (under *Connect*), whenever the game's server is running.
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
5. The **Text client** (*Chat and activity* on the game page, switched to *Text client*; the page remembers which of
   the two you looked at) does what Archipelago's text client does, in the browser: *Connect*, then chat
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
6. **Activity**, the other view of *Chat and activity*, follows what happens in the multiworld, for every player in
   the game: chat, items
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
8. **Your status:** *Set status* next to your slot under *Connect* tells the others how you are doing: *Playing*,
   *Stuck* (waiting for items, "BK"), or *On a break*, with a short note such as "need the Hookshot". Everyone in the
   game sees it next to the slot (also under *Players*) and in the activity, and the Discord channel hears when someone
   is stuck, with where
   the items hinted for them are, so whoever holds one knows (one message per player in ten minutes). Once you reach
   your goal it is no longer shown; giving up the slot clears it.
9. **Handing your slot over:** no time to finish? *Hand over* next to your slot under *Connect* offers your slot to
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
11. **Progress over time** (the game page's *Players* tab) charts every player's share of their locations checked, from
    the first
    check on, with a dot where someone reached their goal. Point at the chart (or focus it and use the arrow keys) for
    everyone's numbers at that moment; pressing a name in the legend brings that player to the front. With more than
    eight players, your own slots are in color and the others gray. While the game runs, the slots below it show each
    player's progress, and when they reached their goal; once it is over, a table of results lists who reached their
    goal first, and after how long, then who got furthest.
12. **Archive:** *Games → Archive* lists the games you were in that are over, *Finished* (every player reached their
    goal) or *Aborted* (moved there before that), with when they ended, the playing time, how many reached their goal,
    and a link to the results. Filter by outcome or search by game, world or slot name.

**Generating your own games** (when an admin lets players generate, *Admin → Settings*): *Generate* works as it does
for admins, with your own configs. So that one player cannot fill the server, a game you
generate has at most 15 worlds, you can have 3 unfinished games of your own at a time (generations still under way
count), and one of them running (a game asleep until its players come back counts as running). *Generate* shows how
many you have left. When a game of yours will not be played to the end, *Move to the archive* on its page lists it as
aborted, stops its server and frees its place; *Back to the running games* takes it out again while you have a place
free (a game everyone finished stays in the archive). A game you
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
outcome, *Use it* to put that outcome into the editor, in place of the weights, without leaving the page; *Save* then
keeps it, to play exactly that one. (Unsaved, the config keeps its weights: every generation rolls them anew.)

**Sign-ups:** open rounds are on the dashboard and under **Sign-ups**. *Join* → pick one of your configs or upload a
YAML. It is checked against the round's rules right away (allowed games, unique player names, how many slots each
player may enter). A file with several players, its documents separated by `---`, takes a slot for each of them, so it
cannot bring more games into a round of one per player. Your entry is a copy: if you edit the config afterwards, the
round page offers to use the new version. You can withdraw until the round closes. *Add to calendar* on a round with a
deadline puts the deadline into your calendar, with a reminder a day before (an hour, if it is closer than that).

## The options form

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

## Language

The web UI is available in English, German (Deutsch), French (Français) and Dutch (Nederlands). It follows the
browser's preferred languages at first; switch it with the language button on the sign-in page or under *Language* in
the user menu (the choice is remembered in that browser). The texts change where they are: the page is neither
loaded nor drawn again, so what you typed, the sections you opened and where you scrolled to stay as they were. Dates and numbers follow the language, in the browser's own
variant of it where there is one (Swiss German, Canadian French, Belgian Dutch, …). Calendar files come in it too.

It is dark, or light when the system asks for that. *Theme* in the user menu picks *Light* or *Dark* instead, for that
browser (its other tabs follow at once); *As on this device* goes back to the system's choice.

## Automatic updates

Pages that show what changes on their own (the dashboard, the games, a game, the sign-ups, a generation) ask the server
for news every few seconds, the game page every 4 seconds, the list of games every 5. A line above such a page says
when it last did. ↻ updates it now; ⏸ pauses the automatic updates, so that the page only changes when you press ↻
(the choice is kept in that browser, and its other tabs follow). A page in a background tab asks nothing, except that
the game page counts new messages for the tab's title every 20 seconds, and catches up when you look at it again.
Signing out, or leaving the page, ends its updates. Pages without the line do not update by themselves. Apart from
that, only what you start yourself asks the server on its own: the text client and the server console while they are
open, the settings page while an Archipelago version installs, and the game page of a game you play, which tells the
server once a minute that you are looking at it (for *While you were away*).

## API tokens

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
