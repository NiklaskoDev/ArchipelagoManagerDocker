# Development

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
- The front end is plain ES modules in `manager/archipelago_manager/static/`, with no build step: what you edit is what
  the browser runs. Texts come from `t()` and `plural()` (`i18n.js`), which give a `Text`: a string that remembers how it
  was made, so that a change of language can rewrite it where it stands. Put it on the page with `h()`, `append()`,
  `setText()` or `setAttribute()`, not with `el.textContent = …`, and make a sentence of several texts with
  `live(() => …)`, not by joining them into a plain string. The image takes the comments and spaces out of the scripts and styles and puts a gzip copy next
  to each text file (`docker/build-assets.mjs`, with esbuild); the server sends that copy to browsers that take it.
  To try that locally: `npm install esbuild && node docker/build-assets.mjs <copy of the static folder>`.
- `ap_helper.py` runs inside Archipelago's own Python environment: it lists worlds, decodes multidata and saves,
  renders templates and describes each game's options for the options form, so the manager never imports Archipelago's
  code itself. `ap_server.py` starts a game's `MultiServer.py` there, with the passwords it reads from the server's
  console. `configs/yamlform.py` turns a YAML into the form's values and back.

## How the code is organized

`manager/archipelago_manager/` is the manager: `main.py` makes the app (middleware, startup, the web UI's files),
`routes/` has the API, one file per area, and the rest is in packages by what it is about. Each package's
`__init__.py` says what is in it.

| Package | What it does |
|---|---|
| `core/` | configuration from the environment, the database, settings, sessions and request guards, the audit log, calendar files |
| `accounts/` | invite and password-reset links, signing in with Discord |
| `archipelago/` | talking to the Archipelago installation (`apbridge.py`, which runs `ap_helper.py`), further versions, custom worlds from GitHub releases, notices of new releases |
| `configs/` | player YAML files: storing them, reading them as the generator does, the options form, *Check*, rolling weights |
| `generation/` | the generation queue (`jobs.py`), saved generation options, what players may generate, sign-up rounds |
| `games/` | one Archipelago server per game (`rooms.py`), importing and storing games, saves and backups, progress and history, the activity feed, the text client, the away summary, spectators, planned starts, reminders, taking games over from archipelago.gg, encrypted connections |
| `notify/` | Discord messages, with their German, French and Dutch texts (`de.py`, `fr.py`, `nl.py`) |

`static/` is the web UI: `index.html`, `style.css`, and in `js/` the scripts: `app.js` (pages and navigation),
`api.js`, `components.js`, `util.js`, `i18n.js` with the dictionaries in `i18n/`, and a module per page in `views/`.
