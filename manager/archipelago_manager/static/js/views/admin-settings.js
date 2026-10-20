import { h, clear, icon, toast, action, fmtBytes } from "../util.js";
import { get, patch, put, post } from "../api.js";
import { pageHeader, field, checkbox, banner, adminTabs, serverOptionsForm, updateBanner, updateStatus } from "../components.js";

export default async function render({ root, cleanup }) {
  const [settings, system] = await Promise.all([get("/api/settings"), get("/api/system")]);

  // general -------------------------------------------------------------------------------------------------------------
  const publicHost = h("input", { class: "input", value: settings.public_host, placeholder: "e.g. archipelago.example.com or 192.168.1.20" });
  const toggles = {
    allow_registration: checkbox("Anyone who can open this page may create an account", settings.allow_registration,
      "Off means only administrators can add users."),
    users_can_start_rooms: checkbox("Players can start, stop and restart the games they are in", settings.users_can_start_rooms),
    users_can_generate: checkbox("Users can generate games from their own configs", settings.users_can_generate),
    users_can_upload_apworlds: checkbox("Users can submit custom games (an administrator still has to approve them)", settings.users_can_upload_apworlds),
    auto_restart_crashed: checkbox("Restart game servers that crash", settings.auto_restart_crashed, "Gives up after three crashes in ten minutes."),
  };
  const rules = serverOptionsForm(settings.default_server_options);
  const saveSettings = h("button", { class: "btn primary", type: "button" }, "Save settings");
  saveSettings.onclick = action(async () => {
    const body = { public_host: publicHost.value.trim(), default_server_options: rules.read() };
    for (const [key, box] of Object.entries(toggles)) body[key] = box.input.checked;
    await patch("/api/settings", body);
    toast("Settings saved", "success");
  });

  // host.yaml -----------------------------------------------------------------------------------------------------------
  const hostArea = h("textarea", { class: "input", rows: 18, spellcheck: false, wrap: "off" });
  const hostPath = h("span", { class: "mono muted" });
  const hostWarning = h("div");
  let hostLoaded = false;
  const hostDetails = h("details", { class: "card" }, h("summary", {}, "Archipelago host.yaml"),
    h("div", { class: "stack" },
      h("p", { class: "muted small" }, "Archipelago's own settings file. Game-specific options, such as base ROM paths needed to generate some games, live here. Per-game server settings above take precedence for servers started by this manager."),
      hostWarning, hostPath, hostArea,
      h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: action(async () => {
        await put("/api/host-yaml", { content: hostArea.value });
        toast("host.yaml saved. Running servers keep their old settings until restarted.", "success");
      }) }, "Save host.yaml"))));
  hostDetails.addEventListener("toggle", action2(async () => {
    if (!hostDetails.open || hostLoaded) return;
    const r = await get("/api/host-yaml");
    hostArea.value = r.content;
    hostPath.textContent = r.path;
    if (!r.persistent) {
      hostWarning.append(banner("warn", icon("alert", 16), "This file is not in the data folder, so edits are lost when the container is recreated. " +
        "Run the container as a regular user (PUID/PGID) to keep them."));
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
        toast(fresh.update_available ? `Archipelago ${fresh.latest} is available` : fresh.error ? fresh.error : "Archipelago is up to date",
          fresh.error ? "error" : "success");
      }) }, "Check now") : null));
    clear(updateBannerHost).append(updateBanner(update) || "");
  };
  drawUpdate(system.update);
  const systemCard = h("div", { class: "card stack" }, h("h2", {}, "System"), updateBannerHost,
    h("dl", { class: "kv" },
      h("dt", {}, "Manager"), h("dd", {}, system.manager_version),
      h("dt", {}, "Archipelago"), h("dd", {}, system.ap_version ? `${system.ap_version} (${system.world_count} games loaded)` : h("span", { class: "form-error" }, system.ap_error || "unavailable")),
      h("dt", {}, "Updates"), updateHost,
      h("dt", {}, "Game ports"), h("dd", {}, `${system.port_range[0]}–${system.port_range[1]}`, h("span", { class: "muted" }, " (each must be published by Docker)")),
      h("dt", {}, "Data folder"), h("dd", {}, h("span", { class: "mono" }, system.data_dir), h("span", { class: "muted" }, ` · ${fmtBytes(system.data_bytes)} used, ${fmtBytes(system.disk.free)} free`)),
      h("dt", {}, "Custom worlds folder"), h("dd", { class: "mono" }, system.worlds_folder || "—")),
    system.quarantined?.length ? banner("warn", icon("alert", 16), h("div", {}, h("strong", {}, "Files moved out of the way:"),
      h("div", { class: "small" }, "These .apworld files in the worlds folder were not valid archives and would have stopped Archipelago from starting."),
      system.quarantined.map((q) => h("div", { class: "small mono" }, q)))) : null,
    failed.length ? banner("warn", icon("alert", 16), h("div", {}, h("strong", {}, "Some worlds failed to load:"),
      failed.map(([n, why]) => h("div", { class: "small mono" }, `${n}: ${String(why).trim().split("\n").pop()}`)))) : null,
    h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: action(async () => {
      const r = await post("/api/system/reload-worlds");
      toast(`${r.world_count} games loaded`, "success");
    }) }, icon("restart", 14), "Reload games")));

  root.append(pageHeader("Administration", "Server-wide settings.", saveSettings), adminTabs("settings"),
    h("div", { class: "stack" },
      h("div", { class: "card stack" }, h("h2", {}, "General"),
        field("Public address", publicHost, "What players type to reach this server. Leave empty to use the address of this page."),
        h("div", { class: "stack tight" }, Object.values(toggles))),
      h("div", { class: "card stack" }, h("h2", {}, "Defaults for new games"),
        h("p", { class: "muted small" }, "Applied when a game is added. Each game can override them."), rules.node),
      hostDetails, systemCard));
}

function action2(fn) {
  return async (e) => { try { await fn(e); } catch (err) { toast(err.message, "error"); } };
}
