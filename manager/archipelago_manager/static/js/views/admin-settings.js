import { h, clear, icon, toast, action, confirmDialog, fmtBytes, fmtDate } from "../util.js";
import { get, patch, put, post, del, refreshStatus } from "../api.js";
import { pageHeader, field, checkbox, banner, adminTabs, serverOptionsForm, updateBanner, updateStatus, copyField, select, romsCard, deliveryStatus, tlsBanner } from "../components.js";
import { t, tServer, plural, LANGUAGES } from "../i18n.js";

const CALLBACK_PATH = "/api/auth/discord/callback";

export default async function render({ root, cleanup }) {
  const [settings, system] = await Promise.all([get("/api/settings"), get("/api/system")]);

  // general -------------------------------------------------------------------------------------------------------------
  const publicHost = h("input", { class: "input", value: settings.public_host, placeholder: t("e.g. archipelago.example.com or 192.168.1.20") });
  const siteUrl = h("input", { class: "input", type: "url", value: settings.site_url, placeholder: location.origin });
  const useThisPage = h("button", { class: "btn sm", type: "button" }, t("Use this page's address"));
  const toggles = {
    allow_registration: checkbox(t("Anyone who can open this page may create an account"), settings.allow_registration,
      t("Off means only administrators can add users.")),
    users_can_start_rooms: checkbox(t("Players can start, stop and restart the games they are in"), settings.users_can_start_rooms),
    users_can_generate: checkbox(t("Users can generate games from their own configs"), settings.users_can_generate),
    users_can_upload_apworlds: checkbox(t("Users can submit custom games (an administrator still has to approve them)"), settings.users_can_upload_apworlds),
    auto_restart_crashed: checkbox(t("Restart game servers that crash"), settings.auto_restart_crashed, t("Gives up after three crashes in ten minutes.")),
  };
  const rules = serverOptionsForm(settings.default_server_options);
  // after how many days without playing, while the others play on, a player is reminded (reminders.py)
  const remindDays = h("input", { class: "input", type: "number", min: 0, max: 60, value: settings.remind_days });
  const discord = notificationsCard(settings);
  const login = discordLoginCard(settings, siteUrl);
  const encryption = tlsCard(settings);
  useThisPage.onclick = () => { siteUrl.value = location.origin; login.siteChanged(); };
  const saveSettings = h("button", { class: "btn primary", type: "button" }, t("Save settings"));
  saveSettings.onclick = action(async () => {
    const body = { public_host: publicHost.value.trim(), site_url: siteUrl.value.trim(), default_server_options: rules.read(),
      remind_days: Number(remindDays.value || 0), ...discord.read(), ...login.read(), ...encryption.read() };
    for (const [key, box] of Object.entries(toggles)) body[key] = box.input.checked;
    const saved = await patch("/api/settings", body);
    discord.drawStatus(saved.notify_status);
    login.saved(saved);
    encryption.drawStatus(saved.tls_status);
    refreshStatus().catch(() => {});  // what players may ask to be mentioned for, in the user menu
    toast(t("Settings saved"), "success");
  });

  // host.yaml -----------------------------------------------------------------------------------------------------------
  const hostArea = h("textarea", { class: "input", rows: 18, spellcheck: false, wrap: "off" });
  const hostPath = h("span", { class: "mono muted" });
  const hostWarning = h("div");
  let hostLoaded = false;
  const hostDetails = h("details", { class: "card" }, h("summary", {}, t("Archipelago host.yaml")),
    h("div", { class: "stack" },
      h("p", { class: "muted small" }, t("Archipelago's own settings file. Game-specific options, such as base ROM paths needed to generate some games, live here. Per-game server settings above take precedence for servers started by this manager.")),
      hostWarning, hostPath, hostArea,
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: action(async () => {
        await put("/api/host-yaml", { content: hostArea.value });
        toast(t("host.yaml saved. Running servers keep their old settings until restarted."), "success");
      }) }, t("Save host.yaml")))));
  hostDetails.addEventListener("toggle", action2(async () => {
    if (!hostDetails.open || hostLoaded) return;
    const r = await get("/api/host-yaml");
    hostArea.value = r.content;
    hostPath.textContent = r.path;
    if (!r.persistent) {
      hostWarning.append(banner("warn", icon("alert", 16), t("This file is not in the data folder, so edits are lost when the container is recreated. Run the container as a regular user (PUID/PGID) to keep them.")));
    }
    hostLoaded = true;
  }));

  // system --------------------------------------------------------------------------------------------------------------
  const failed = Object.entries(system.failed_worlds || {});
  const updateHost = h("dd");
  const updateBannerHost = h("div");
  const drawUpdate = (update) => {
    clear(updateHost).append(h("div", { class: "row" }, updateStatus(update),
      update.enabled ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
        const fresh = await post("/api/system/check-updates");
        drawUpdate(fresh);
        toast(fresh.update_available ? t("Archipelago {version} is available", { version: fresh.latest })
          : fresh.error ? tServer(fresh.error) : t("Archipelago is up to date"), fresh.error ? "error" : "success");
      }) }, t("Check now")) : null));
    clear(updateBannerHost).append(updateBanner(update) || "");
  };
  drawUpdate(system.update);
  const systemCard = h("div", { class: "card stack" }, h("h2", {}, t("System")), updateBannerHost,
    h("dl", { class: "kv" },
      h("dt", {}, t("Manager")), h("dd", {}, system.manager_version),
      h("dt", {}, "Archipelago"), h("dd", {}, system.ap_version
        ? `${system.ap_version} (${plural(system.world_count, "{n} game loaded", "{n} games loaded")})`
        : h("span", { class: "form-error" }, system.ap_error ? tServer(system.ap_error) : t("unavailable"))),
      h("dt", {}, t("Updates")), updateHost,
      h("dt", {}, t("Game ports")), h("dd", {}, `${system.port_range[0]}–${system.port_range[1]}`, h("span", { class: "muted" }, " ", t("(each must be published by Docker)"))),
      h("dt", {}, t("Data folder")), h("dd", {}, h("span", { class: "mono" }, system.data_dir), h("span", { class: "muted" }, " · ",
        t("{used} used, {free} free", { used: fmtBytes(system.data_bytes), free: fmtBytes(system.disk.free) }))),
      h("dt", {}, t("Custom worlds folder")), h("dd", { class: "mono" }, system.worlds_folder || "—")),
    system.quarantined?.length ? banner("warn", icon("alert", 16), h("div", {}, h("strong", {}, t("Files moved out of the way:")),
      h("div", { class: "small" }, t("These .apworld files in the worlds folder were not valid archives and would have stopped Archipelago from starting.")),
      system.quarantined.map((q) => h("div", { class: "small mono" }, q)))) : null,
    failed.length ? banner("warn", icon("alert", 16), h("div", {}, h("strong", {}, t("Some worlds failed to load:")),
      failed.map(([n, why]) => h("div", { class: "small mono" }, `${n}: ${String(why).trim().split("\n").pop()}`)))) : null,
    h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: action(async () => {
      const r = await post("/api/system/reload-worlds");
      toast(plural(r.world_count, "{n} game loaded", "{n} games loaded"), "success");
    }) }, icon("restart", 14), t("Reload games"))));

  root.append(pageHeader(t("Administration"), t("Server-wide settings."), saveSettings), adminTabs("settings"),
    h("div", { class: "stack" },
      h("div", { class: "card stack" }, h("h2", {}, t("General")),
        field(t("Public address"), publicHost, t("What players type to reach this server. Leave empty to use the address of this page.")),
        field(t("Address of this web UI"), h("div", { class: "row" }, h("div", { class: "grow" }, siteUrl), useThisPage),
          t("Where players open this page, e.g. https://archipelago.example.com. Discord messages link here, and signing in with Discord needs it.")),
        h("div", { class: "stack tight" }, Object.values(toggles))),
      encryption.node,
      h("div", { class: "card stack" }, h("h2", {}, t("Defaults for new games")),
        h("p", { class: "muted small" }, t("Applied when a game is added. Each game can override them.")), rules.node,
        field(t("Remind players who stopped playing after (days)"), remindDays,
          t("For every game that does not set its own: a player who has not played for this many days while the others play on is reminded on their dashboard and on Discord, up to three times. 0 turns it off."))),
      login.node,
      discord.node,
      romsCard(),
      backupCard(),
      versionsCard(cleanup),
      hostDetails, systemCard));
}

// Further Archipelago versions, for games that need another one than this server comes with (versions.py)
function versionsCard(cleanup) {
  const host = h("div", { class: "stack" });
  const input = h("input", { class: "input", placeholder: t("e.g. 0.6.2"), autocomplete: "off", style: { maxWidth: "160px" },
    "aria-label": t("Version to install") });
  const install = h("button", { class: "btn", type: "button" }, icon("download", 14), t("Install"));
  const logs = {};  // the log of the versions whose log is open, kept across redraws
  let timer = null;
  cleanup(() => clearTimeout(timer));
  const STATUS = { installing: ["warn", () => t("Installing…")], installed: ["ok", () => t("Installed")],
    failed: ["bad", () => t("Failed")], missing: ["bad", () => t("Files missing")] };

  function logView(version) {
    const pre = h("pre", { class: "console", style: { maxHeight: "260px", margin: 0 } }, logs[version] ?? t("Loading…"));
    const details = h("details", {}, h("summary", { class: "muted small" }, t("Log")), pre);
    if (version in logs) details.open = true;
    details.addEventListener("toggle", action2(async () => {
      if (!details.open) { delete logs[version]; return; }
      logs[version] = await get(`/api/ap-versions/${version}/log`);
      pre.textContent = logs[version];
      pre.scrollTop = pre.scrollHeight;
    }));
    return details;
  }

  function draw(data) {
    const rows = data.versions.map((v) => {
      const [kind, label] = STATUS[v.status];
      return h("div", { class: "card stack tight", style: { padding: "12px 14px" } },
        h("div", { class: "row spread" },
          h("div", { class: "row" }, h("strong", {}, `Archipelago ${v.version}`), h("span", { class: `badge ${kind}` }, label()),
            v.size ? h("span", { class: "muted small" }, fmtBytes(v.size)) : null,
            v.world_count ? h("span", { class: "muted small" }, plural(v.world_count, "{n} game loaded", "{n} games loaded")) : null),
          h("div", { class: "row" },
            ["failed", "missing"].includes(v.status) ? h("button", { class: "btn sm", type: "button", onclick: action(async () => {
              draw(await post("/api/ap-versions", { version: v.version }));
            }) }, icon("restart", 14), t("Install again")) : null,
            v.status !== "installing" ? h("button", { class: "btn sm ghost icon-only", type: "button", title: t("Remove"), "aria-label": t("Remove"),
              onclick: action(async () => {
                if (!(await confirmDialog(t("Its files are deleted. Games cannot run on it any more until it is installed again."),
                  { title: t("Remove Archipelago {version}?", { version: v.version }), danger: true, confirmText: t("Remove") }))) return;
                draw(await del(`/api/ap-versions/${v.version}`));
                toast(t("Archipelago {version} removed", { version: v.version }), "success");
              }) }, icon("trash", 14)) : null)),
        v.error ? h("div", { class: "form-error small" }, tServer(v.error)) : null,
        v.status === "missing" ? h("div", { class: "muted small" }, t("Its files are not in the data folder (backups leave them out). Install it again.")) : null,
        v.games.length ? h("div", { class: "muted small" }, t("Used by: {games}", { games: v.games.join(", ") })) : null,
        v.failed_worlds.length ? h("div", { class: "muted small" }, t("Did not load: {games}", { games: v.failed_worlds.join(", ") })) : null,
        v.installed_at && v.status === "installed" ? h("div", { class: "muted small" }, t("Installed {date}", { date: fmtDate(v.installed_at) })) : null,
        logView(v.version));
    });
    clear(host).append(rows.length ? rows : h("p", { class: "muted" }, t("Only the version this server comes with.")));
    const busy = data.versions.some((v) => v.status === "installing");
    install.disabled = busy;
    clearTimeout(timer);
    if (busy) {
      timer = setTimeout(async () => {
        const fresh = await get("/api/ap-versions").catch(() => null);
        for (const version of Object.keys(logs)) logs[version] = await get(`/api/ap-versions/${version}/log`).catch(() => logs[version]);
        if (fresh && host.isConnected) draw(fresh);
      }, 3000);
    }
  }

  install.onclick = action(async () => {
    const version = input.value.trim();
    if (!version) throw new Error(t("Enter the version to install, e.g. 0.6.2"));
    logs[version.replace(/^v/, "")] = "";
    draw(await post("/api/ap-versions", { version }));
    input.value = "";
    toast(t("Installing Archipelago {version}. This takes a few minutes.", { version }), "success");
  });
  input.onkeydown = (e) => { if (e.key === "Enter") install.click(); };
  get("/api/ap-versions").then(draw).catch((err) => clear(host).append(banner("error", icon("alert", 16), err.message)));

  return h("div", { class: "card stack" }, h("h2", {}, t("Archipelago versions")),
    h("p", { class: "muted small" }, t("Games run on the version this server comes with, unless their settings name another one. Install further versions here, for seeds or custom games that need an older (or newer) Archipelago: each is downloaded from GitHub with its requirements, which takes a few minutes and some hundred megabytes. Custom games are added to every version they say they work with; base ROMs are shared. Backups of everything leave the versions out: install them again after a restore.")),
    host, h("div", { class: "row" }, input, install));
}

const EVENTS = [
  ["game_online", t("A game goes online"), t("When someone starts it; not when it comes back after a crash or a restart of the container.")],
  ["game_crashed", t("A game server crashes")],
  ["player_goal", t("A player reaches their goal, and when everyone has")],
  ["player_stuck", t("A player says they are stuck"), t("With their note and where the items hinted for them are.")],
  ["player_inactive", t("A player has not played for a while, and the others play on"),
    t("With how many hinted items of the others wait in their world. When this happens is set under Defaults for new games, and per game.")],
  ["slot_handover", t("A player hands their slot over, and someone takes it over"),
    t("A slot offered to whoever takes it is announced to everyone who can see the game.")],
  ["hint_found", t("A hinted item is found"), t("Many at once, for example after a !release, come as one message.")],
  ["generation", t("A new game is generated, or generating fails")],
  ["signup", t("Sign-up rounds open, close soon and close")],
  ["apworld_pending", t("A custom game or GitHub repository waits for approval")],
  ["apworld_update", t("A custom game from GitHub has a new release")],
  ["ap_update", t("A new Archipelago version is out")],
];

// Discord webhook settings. Saved with the page's "Save settings" button; read() returns their part of the request.
function notificationsCard(settings) {
  const webhook = h("input", { class: "input", type: "url", value: settings.discord_webhook_url, autocomplete: "off",
    placeholder: "https://discord.com/api/webhooks/…" });
  const language = select(Object.entries(LANGUAGES), settings.notify_language);
  const boxes = EVENTS.map(([key, label, hint]) => ({ key, box: checkbox(label, settings.notify_events.includes(key), hint) }));
  const mentions = checkbox(t("Let players be mentioned"), settings.notify_mentions,
    t("Players who linked their Discord account choose in their user menu what they want to be mentioned for, such as their game going online. Nobody else is ever mentioned, not @everyone and no role."));
  const statusHost = h("div");
  const drawStatus = (status) => clear(statusHost).append(deliveryStatus(status) || "");
  drawStatus(settings.notify_status);
  const test = h("button", { class: "btn", type: "button" }, icon("bell", 14), t("Send a test message"));
  test.onclick = action(async () => {
    const result = await post("/api/settings/test-notification", { discord_webhook_url: webhook.value.trim() || null });
    drawStatus(result.notify_status);
    toast(t("Test message sent. Check your Discord channel."), "success");
  });
  const node = h("div", { class: "card stack" }, h("h2", {}, t("Discord notifications")),
    h("p", { class: "muted small" }, t("Posts a message to a Discord channel when something happens. In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. Leave the address empty to turn notifications off.")),
    field(t("Webhook address"), webhook),
    field(t("Language of the messages"), language),
    h("div", { class: "stack tight" }, h("span", { class: "field-label" }, t("Send a message when…")), boxes.map((b) => b.box)),
    mentions,
    statusHost,
    h("div", { class: "row" }, test));
  const read = () => ({
    discord_webhook_url: webhook.value.trim(), notify_language: language.value,
    notify_events: boxes.filter((b) => b.box.input.checked).map((b) => b.key),
    notify_mentions: mentions.input.checked,
  });
  return { node, read, drawStatus };
}

// The certificate the game servers present for encrypted connections (tls.py). Saved with "Save settings".
function tlsCard(settings) {
  const cert = h("input", { class: "input mono", value: settings.tls_cert, autocomplete: "off", spellcheck: false,
    placeholder: "/config/tls/fullchain.pem" });
  const key = h("input", { class: "input mono", value: settings.tls_key, autocomplete: "off", spellcheck: false,
    placeholder: "/config/tls/privkey.pem" });
  const statusHost = h("div", { class: "stack tight" });
  const drawStatus = (status) => {
    clear(statusHost);
    if (!status.enabled) {
      statusHost.append(h("p", { class: "muted small" }, t("Off: games are served unencrypted.")));
      return;
    }
    const problem = tlsBanner(status);
    if (problem) statusHost.append(problem);
    if (!status.ok) return;
    statusHost.append(h("dl", { class: "kv" },
      h("dt", {}, t("For")), h("dd", {}, status.names.join(", ")),
      h("dt", {}, t("Valid until")), h("dd", {}, fmtDate(status.expires_at)),
      h("dt", {}, t("Issued by")), h("dd", {}, status.self_signed ? t("itself (self-signed)") : status.issuer || "—")));
    if (status.self_signed) {
      statusHost.append(banner("warn", icon("alert", 16), t("Archipelago's clients only accept certificates of a known authority, such as Let's Encrypt. With a self-signed one, players cannot connect.")));
    }
    if (status.covers_public_host === false) {
      statusHost.append(banner("warn", icon("alert", 16), t("The certificate is not for the public address {host}, so players' clients will refuse it. Set the public address to one of the names it is for.", { host: settings.public_host })));
    }
    if (status.games_without) {
      statusHost.append(h("p", { class: "muted small" }, plural(status.games_without,
        "{n} game is set to run without encryption.", "{n} games are set to run without encryption.")));
    }
  };
  drawStatus(settings.tls_status);
  const node = h("div", { class: "card stack" }, h("h2", {}, t("Encrypted game connections")),
    h("p", { class: "muted small" }, t("With a certificate, the game servers accept encrypted connections only (wss://), so passwords and chat do not cross the internet readable. Archipelago's clients switch to it by themselves; players keep entering the same address. The certificate must be for the public address, from an authority such as Let's Encrypt: mount the folder your reverse proxy keeps it in into the container (read only), and enter the paths as the container sees them. Renewed certificates are taken over by the running servers once nobody is connected.")),
    h("div", { class: "form-grid" },
      field(t("Certificate"), cert, t("PEM file with the certificate and its chain, e.g. fullchain.pem")),
      field(t("Private key"), key, t("PEM file without a password, e.g. privkey.pem. Empty if it is in the certificate's file."))),
    h("p", { class: "muted small" }, t("Leave both empty to turn encryption off.")),
    statusHost);
  const read = () => ({ tls_cert: cert.value.trim(), tls_key: key.value.trim() });
  return { node, read, drawStatus };
}

// The whole data folder as one zip. The server makes it first (a POST, so another site cannot set it off), then the
// browser downloads it once from the address it gets back.
function backupCard() {
  const make = h("button", { class: "btn", type: "button" }, icon("download", 14), t("Download a backup"));
  const status = h("span", { class: "muted small" });
  make.onclick = action(async () => {
    status.textContent = t("Making the backup…");
    try {
      const made = await post("/api/system/backup");
      status.textContent = t("Backup made ({size}). The download starts now.", { size: fmtBytes(made.size) });
      const link = h("a", { href: made.url, download: "" });
      document.body.append(link);
      link.click();
      link.remove();
    } catch (err) {
      status.textContent = "";
      throw err;
    }
  });
  return h("div", { class: "card stack" }, h("h2", {}, t("Backup of everything")),
    h("p", { class: "muted small" }, t("One zip of the whole data folder: users, configs, games and their saves, custom games, sign-up rounds and the settings. It holds passwords (hashed) and the Discord secrets, so keep it safe. To restore it, stop the container, put the zip's contents into an empty data folder and start the container again.")),
    h("div", { class: "row" }, make, status));
}

// Signing in with Discord (OAuth2). The client secret never comes back from the server: the field stays empty and is
// only sent when something is typed into it.
function discordLoginCard(settings, siteUrl) {
  const enabled = checkbox(t("Let people sign in with Discord"), settings.discord_login);
  const clientId = h("input", { class: "input", value: settings.discord_client_id, inputMode: "numeric", autocomplete: "off",
    placeholder: "123456789012345678" });
  const secret = h("input", { class: "input", type: "password", autocomplete: "new-password", spellcheck: false });
  const signup = checkbox(t("Create an account for people who sign in with Discord for the first time"), settings.discord_signup,
    t("Off means only people who already have an account here and linked Discord to it in their user menu can sign in this way."));
  const guild = h("input", { class: "input", value: settings.discord_guild_id, inputMode: "numeric", autocomplete: "off",
    placeholder: t("optional") });
  const redirectHost = h("div");
  const secretHint = h("span");
  const drawSecretHint = (isSet) => {
    secret.placeholder = isSet ? "••••••••••••" : "";
    secretHint.textContent = isSet ? t("Saved, and never shown again. Type a new one to replace it.") : t("Not saved yet.");
  };
  const siteChanged = () => {
    const base = siteUrl.value.trim().replace(/\/+$/, "");
    clear(redirectHost).append(/^https?:\/\/[^/]/.test(base)
      ? copyField(base + CALLBACK_PATH)
      : h("span", { class: "muted small" }, t("Enter the address of this web UI under General first.")));
  };
  siteUrl.addEventListener("input", siteChanged);
  siteChanged();
  drawSecretHint(settings.discord_client_secret_set);
  const node = h("div", { class: "card stack" }, h("h2", {}, t("Sign in with Discord")),
    h("p", { class: "muted small" }, t("In the Discord Developer Portal (discord.com/developers/applications): create an application, add the redirect address below under OAuth2 → Redirects, then copy the client ID and the client secret from OAuth2 into the fields here.")),
    enabled,
    field(t("Redirect address"), redirectHost, t("Discord only sends people back to this exact address.")),
    h("div", { class: "form-grid" },
      field(t("Client ID"), clientId),
      field(t("Client secret"), secret, secretHint)),
    signup,
    field(t("Only members of this Discord server"), guild,
      t("The server's ID: in Discord, right-click the server → Copy Server ID (turn on Developer Mode in the advanced settings first). Leave empty to allow any Discord account.")));
  const read = () => {
    const body = { discord_login: enabled.input.checked, discord_client_id: clientId.value.trim(),
      discord_signup: signup.input.checked, discord_guild_id: guild.value.trim() };
    if (secret.value) body.discord_client_secret = secret.value.trim();
    return body;
  };
  const saved = (values) => {
    secret.value = "";
    drawSecretHint(values.discord_client_secret_set);
  };
  return { node, read, saved, siteChanged };
}

function action2(fn) {
  return async (e) => { try { await fn(e); } catch (err) { toast(err.message, "error"); } };
}
