// Browser walkthrough of the web UI against a running manager that has Archipelago and NO users yet.
//
//   BASE=http://127.0.0.1:8080 FIXTURES=/path/to/fixtures SHOTS=/tmp/shots node tests/ui/flows.mjs
//
// FIXTURES must contain customchecks.apworld and a pregenerated zip named pregenerated.zip (see tests/helpers.py for
// how the integration tests build them). Playwright is resolved from NODE_PATH or the global node_modules.
import { createRequire } from "module";
import fs from "fs";

const require = createRequire(process.env.NODE_PLAYWRIGHT_DIR || "/opt/node22/lib/node_modules/");
const { chromium } = require("playwright");
const BASE = process.env.BASE || "http://127.0.0.1:8080";
const FIXTURES = process.env.FIXTURES;
const SHOTS = process.env.SHOTS || "/tmp/ui-shots";
fs.mkdirSync(SHOTS, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM || "/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args: ["--no-sandbox"] });
const problems = [];
const pages = [];
const clientErrors = [];
let step = 0;
const log = (msg) => console.log(`  ${String(++step).padStart(2, "0")}. ${msg}`);

async function newPage(name, scheme = "light") {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: scheme, acceptDownloads: true });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => problems.push(`[${name}] pageerror: ${e.message}`));
  page.on("console", (m) => {
    // The browser logs every 4xx response as a console error; the negative steps below provoke some on purpose and
    // are listed at the end for review. Anything else is a failure.
    if (m.type() === "error" && !/Failed to load resource: the server responded with a status of 4\d\d/.test(m.text())) {
      problems.push(`[${name}] console.error: ${m.text()}`);
    }
  });
  page.on("response", (r) => {
    if (r.status() >= 500) problems.push(`[${name}] HTTP ${r.status()} ${r.request().method()} ${r.url()}`);
    else if (r.status() >= 400) clientErrors.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname} (${name})`);
  });
  page.name = name;
  pages.push(page);
  return page;
}
const shot = (page, label) => page.screenshot({ path: `${SHOTS}/${page.name}-${label}.png`, fullPage: true });
const toastText = (page) => page.locator(".toast").last().innerText();
async function expectText(page, text, timeout = 15000) {
  await page.getByText(text, { exact: false }).first().waitFor({ timeout });
}
async function assertNoStrayText(page, label) {
  const text = await page.evaluate(() => document.getElementById("view")?.innerText || "");
  const bad = text.match(/\b(null|undefined|NaN)\b|\[object/g);
  if (bad) problems.push(`[${page.name}] ${label}: stray text ${[...new Set(bad)].join(", ")}`);
}
async function nav(page, hash, label) {
  await page.goto(`${BASE}/${hash}`);
  await page.waitForSelector("#view > *");
  await page.waitForTimeout(500);
  await assertNoStrayText(page, label || hash);
}

try {
// ---------------------------------------------------------------------------------------------------------------------
console.log("admin");
const admin = await newPage("admin");
await admin.goto(BASE);
await admin.waitForSelector("form");
await expectText(admin, "Create the administrator account");
log("first run asks for an administrator");
await admin.fill("input[autocomplete=username]", "boss");
const pw = admin.locator("input[type=password]");
await pw.nth(0).fill("bosspass123");
await pw.nth(1).fill("different");
await admin.click("button[type=submit]");
await expectText(admin, "do not match");
await pw.nth(1).fill("bosspass123");
await admin.click("button[type=submit]");
await admin.waitForSelector(".topbar");
await expectText(admin, "Welcome, boss");
log("administrator created, password mismatch rejected first");

// users ---------------------------------------------------------------------------------------------------------------
await nav(admin, "#/admin/users");
await admin.getByRole("button", { name: "Add user" }).click();
let dialog = admin.locator(".modal");
await dialog.locator("input").nth(0).fill("gwen");
await dialog.locator("input").nth(1).fill("short");
await dialog.getByRole("button", { name: "Create user" }).click();
await expectText(admin, "at least 8");
await dialog.locator("input").nth(1).fill("gwenpass123");
await dialog.getByRole("button", { name: "Create user" }).click();
await admin.locator("tbody tr", { hasText: "gwen" }).waitFor();
log("user created via dialog (short password refused first)");
await admin.getByRole("button", { name: "Add user" }).click();
dialog = admin.locator(".modal");
await dialog.locator("input").nth(0).fill("garth");
await dialog.locator("input").nth(1).fill("garthpass123");
await dialog.getByRole("button", { name: "Create user" }).click();
await admin.locator("tbody tr", { hasText: "garth" }).waitFor();

// configs -------------------------------------------------------------------------------------------------------------
await nav(admin, "#/configs");
const yamlDir = `${SHOTS}/yaml`;
fs.mkdirSync(yamlDir, { recursive: true });
fs.writeFileSync(`${yamlDir}/gwen.yaml`, "name: Gwen\ngame: ChecksFinder\nChecksFinder: {}\n");
fs.writeFileSync(`${yamlDir}/broken.yaml`, "name: Broken\ngame: [oops\n");
fs.writeFileSync(`${yamlDir}/custom.yaml`, "name: Cust\ngame: Custom Checks\nCustom Checks: {}\n");
await admin.setInputFiles("input[type=file]", [`${yamlDir}/gwen.yaml`, `${yamlDir}/broken.yaml`, `${yamlDir}/custom.yaml`]);
await admin.locator("tbody tr", { hasText: "gwen.yaml" }).waitFor();
await expectText(admin, "broken.yaml");  // error toast for the unusable file
log("uploaded 3 files: 2 accepted, the broken one reported");
// a config for a game the server lacks immediately prompts for the APWorld ...
const prompt = admin.locator(".modal", { hasText: "Custom game needed" });
await prompt.waitFor();
await prompt.getByText("Used by config custom.yaml").waitFor();
await shot(admin, "missing-game-prompt");
await prompt.getByRole("button", { name: "Not now" }).click();
await admin.locator(".modal").waitFor({ state: "detached" });
// ... and keeps nagging on the page itself until it is installed
await admin.locator(".banner.warn", { hasText: "Custom Checks" }).waitFor();
await shot(admin, "configs");
log("config for an uninstalled game prompts for the APWorld and stays flagged on the page");

// template picker + editor ----------------------------------------------------------------------------------------------
await admin.getByRole("link", { name: "New from template" }).first().click();
await admin.waitForSelector("a.tile");
await admin.fill("input[type=search]", "checksf");
await admin.locator("a.tile", { hasText: "ChecksFinder" }).click();
await admin.waitForSelector("textarea.editor");
const text = await admin.inputValue("textarea.editor");
if (!text.includes("game: ChecksFinder") || !/^name: boss$/m.test(text)) problems.push("template editor did not prefill name/game");
log("template opens in the editor with the username as player name");
await admin.fill("textarea.editor", "name: Tmpl\ngame: [broken");
await admin.getByRole("button", { name: "Save" }).click();
await admin.locator(".banner.error", { hasText: "Invalid YAML" }).waitFor();  // not the toast left over from the upload
log("saving broken YAML shows the error and does not save");
await admin.fill("textarea.editor", "name: Tmpl\ngame: ChecksFinder\nChecksFinder: {}\n");
await admin.keyboard.press("Control+s");
await admin.waitForURL(/#\/configs\/\d+$/);
await expectText(admin, "Tmpl");
log("Ctrl+S saves and moves to the saved config");
admin.once("dialog", (d) => d.accept());  // the unsaved-changes guard
await admin.fill("textarea.editor", "name: Tmpl\ngame: ChecksFinder\nChecksFinder: {}\n# edited\n");
await admin.getByRole("link", { name: "My configs" }).click();
await admin.waitForURL(/#\/configs$/);
log("leaving with unsaved changes asks for confirmation");

// custom games --------------------------------------------------------------------------------------------------------
await nav(admin, "#/worlds");
await expectText(admin, "No custom games yet");
await expectText(admin, "Supported games");
await admin.getByRole("button", { name: "Add APWorld" }).first().click();
dialog = admin.locator(".modal");
await dialog.locator("input[type=file]").setInputFiles(`${FIXTURES}/customchecks.apworld`);
await dialog.getByRole("button", { name: "Install" }).click();
await admin.locator("tbody tr", { hasText: "Custom Checks" }).waitFor({ timeout: 60000 });
await expectText(admin, "Installed");
await shot(admin, "worlds");
log("APWorld installed from the UI and listed as Installed");
await nav(admin, "#/configs");
if (await admin.locator(".banner.warn", { hasText: "Custom Checks" }).count()) problems.push("missing-game prompt still shown after installing the world");
log("the 'not installed' prompt is gone after installing");

// generate ------------------------------------------------------------------------------------------------------------
await nav(admin, "#/generate");
await admin.locator("tbody tr", { hasText: "gwen.yaml" }).click();
await admin.locator("tbody tr", { hasText: "custom.yaml" }).click();
await expectText(admin, "2 selected");
await admin.fill("input[placeholder^='Optional']", "UI run");
await shot(admin, "generate");
await admin.getByRole("button", { name: "Generate" }).click();
await admin.waitForURL(/#\/jobs\/\d+$/);
await admin.locator(".badge", { hasText: "Done" }).waitFor({ timeout: 120000 });
await expectText(admin, "Imported as game");
await shot(admin, "job");
log("job ran to completion with a live log");
await admin.getByRole("link", { name: "Open game" }).click();
await admin.waitForURL(/#\/games\/\d+$/);
const gameUrl = admin.url();
await expectText(admin, "UI run");
log("result opens as a game");

// game page ------------------------------------------------------------------------------------------------------------
await admin.locator(".page-actions").getByRole("button", { name: "Start" }).click();
await admin.locator(".badge", { hasText: "Running" }).first().waitFor({ timeout: 60000 });
log("server starts from the UI");
const ownerSelects = admin.locator("table").first().locator("select");  // the slots table; Files has selects too
if (await ownerSelects.count() !== 2) problems.push(`expected 2 owner selects, got ${await ownerSelects.count()}`);
const ownerValues = await ownerSelects.evaluateAll((els) => els.map((e) => e.selectedOptions[0].textContent));
if (ownerValues.some((v) => !v.includes("boss"))) problems.push(`slots should belong to boss, got ${ownerValues}`);
// give the second slot to gwen
await ownerSelects.nth(0).selectOption({ label: "gwen" });
await admin.waitForTimeout(800);
await admin.locator("details", { hasText: "Settings" }).first().locator("summary").click();
await admin.locator("details[open] input").first().fill("UI run (renamed)");
await admin.locator("details[open] input[placeholder='none']").first().fill("room1");
await admin.getByRole("button", { name: "Save settings" }).click();
await expectText(admin, "Settings saved");
await expectText(admin, "UI run (renamed)");
log("owner assignment and settings save work");
await admin.locator("details", { hasText: "Console" }).first().locator("summary").click();
await admin.locator(".console").getByText("starting server").first().waitFor({ timeout: 10000 });
await admin.fill("input[placeholder^='Server command']", "/help");
await admin.keyboard.press("Enter");
await admin.locator(".console .cmd", { hasText: "/help" }).waitFor({ timeout: 10000 });
log("console shows the log and accepts commands");
await shot(admin, "game");

// pregenerated upload ---------------------------------------------------------------------------------------------------
await nav(admin, "#/games");
await admin.getByRole("button", { name: "Add a game" }).first().click();
dialog = admin.locator(".modal");
await dialog.locator("input[type=file]").setInputFiles(`${FIXTURES}/pregenerated.zip`);
await dialog.locator("input").nth(1).fill("Pregenerated via UI");
await dialog.locator("input[type=checkbox]").uncheck();
await dialog.getByRole("button", { name: "Upload and add" }).click();
await admin.waitForURL(/#\/games\/\d+$/, { timeout: 60000 });
await expectText(admin, "Pregenerated via UI");
await admin.locator("details", { hasText: "Files" }).first().locator("summary").click();
await expectText(admin, "README.txt");
log("pregenerated zip uploaded; patch files extracted per slot");
await shot(admin, "pregenerated");
// allow claiming
await admin.locator("details", { hasText: "Settings" }).first().locator("summary").click();
await admin.getByLabel("Let users claim unowned slots themselves").check();
await admin.getByLabel("Players may claim only one slot each").check();
await admin.locator("select").filter({ hasText: "Only players in the game" }).selectOption("all");
await admin.getByRole("button", { name: "Save settings" }).click();
await expectText(admin, "Settings saved");
log("claiming (one slot each) enabled and game made visible to everyone");

// delete via danger zone --------------------------------------------------------------------------------------------------
await admin.locator("details", { hasText: "Danger zone" }).first().locator("summary").click();
const claimGameUrl = admin.url();

// ---------------------------------------------------------------------------------------------------------------------
console.log("regular user (dark theme)");
const user = await newPage("gwen", "dark");
await user.goto(BASE);
await user.waitForSelector("form");
await user.fill("input[autocomplete=username]", "garth");
await user.fill("input[type=password]", "garthpass123");
await user.click("button[type=submit]");
await user.waitForSelector(".topbar");
log("regular user signs in");
if (await user.getByRole("link", { name: "Admin", exact: true }).count()) problems.push("regular user can see the Admin link");
if (await user.getByRole("link", { name: "Generate", exact: true }).count()) problems.push("regular user can see Generate while it is disabled");
await nav(user, claimGameUrl.slice(BASE.length + 1));
await expectText(user, "Pregenerated via UI");
const claim = user.getByRole("button", { name: "This is mine" }).first();
await claim.waitFor();
await shot(user, "claimable");
await claim.click();
await expectText(user, "You now own");
await user.locator("table a[href*='/files/']").first().waitFor();  // the claimed slot's patch file
log("user claims a slot and gets their files");
await expectText(user, "Each player can claim one slot");
if (await user.getByRole("button", { name: "This is mine" }).count()) problems.push("a second slot is still offered for claiming");
log("with one slot each, no further slot is offered");
if (await user.locator("details", { hasText: "Console" }).count()) problems.push("regular user can see the console");
await shot(user, "claimed");
await nav(user, "#/");
await expectText(user, "Your games");
await nav(user, "#/worlds");
await expectText(user, "reviewed by an administrator");
await user.getByRole("button", { name: "Submit APWorld" }).first().click();
await expectText(user, "approve it before");
await user.keyboard.press("Escape");
await nav(user, "#/admin/users");  // not allowed: the page must fail gracefully
await expectText(user, "Administrator access required");
log("admin-only page refuses the user with a readable message");
await user.setViewportSize({ width: 390, height: 800 });
await nav(user, "#/");
await shot(user, "mobile");
log("mobile viewport renders");

// ---------------------------------------------------------------------------------------------------------------------
console.log("sign-up round");
await user.setViewportSize({ width: 1280, height: 900 });
await nav(admin, "#/signups");
await expectText(admin, "No sign-up rounds yet");
await admin.getByRole("button", { name: "New round" }).first().click();
dialog = admin.locator(".modal");
await dialog.locator("input").first().fill("UI round");
await dialog.locator("input[list]").fill("checksfinder");
await dialog.locator("input[list]").press("Enter");
await dialog.locator(".chip", { hasText: "ChecksFinder" }).waitFor();
await dialog.getByLabel("No deadline: close the sign-ups by hand").check();
await dialog.getByRole("button", { name: "Open sign-ups" }).click();
await admin.waitForURL(/#\/signups\/\d+$/);
const roundUrl = admin.url();
await expectText(admin, "Sign-ups are open");
log("admin opens a sign-up round limited to ChecksFinder");
await nav(user, "#/");
await expectText(user, "Open sign-ups");
await user.getByRole("link", { name: "Join" }).first().click();
await user.waitForURL(/#\/signups\/\d+$/);
await user.locator("input[type=file][accept='.yaml,.yml']").setInputFiles({
  name: "garth-round.yaml", mimeType: "text/yaml", buffer: Buffer.from("name: Garth\ngame: ChecksFinder\nChecksFinder: {}\n") });
await expectText(user, "Uploaded to your configs and entered");
await user.locator("h2", { hasText: "Your entry" }).waitFor();
await shot(user, "signup-entered");
log("player joins from the dashboard by uploading a YAML");
await user.getByRole("button", { name: "Withdraw" }).click();
await user.locator(".modal").getByRole("button", { name: "Withdraw" }).click();
await expectText(user, "You have not entered yet");
await user.locator(".card select").selectOption({ label: "garth-round.yaml (Garth · ChecksFinder)" });
await user.getByRole("button", { name: "Enter", exact: true }).click();
await expectText(user, "garth-round.yaml · entered");
log("player withdraws and enters again from their library");
await nav(admin, roundUrl.slice(BASE.length + 1));
await admin.locator("tbody tr", { hasText: "garth" }).waitFor();
await admin.getByRole("button", { name: "Close now" }).click();
await admin.locator(".modal").getByRole("button", { name: "Close sign-ups" }).click();
await admin.getByText("The game is ready").waitFor({ timeout: 120000 });
await shot(admin, "signup-done");
log("closing the round generates the game");
await nav(user, roundUrl.slice(BASE.length + 1));
await user.getByRole("link", { name: "Open the game" }).click();
await user.waitForURL(/#\/games\/\d+$/);
await expectText(user, "Your slot");
log("the player is in the generated game");

// ---------------------------------------------------------------------------------------------------------------------
console.log("settings persist");
await nav(admin, "#/admin/settings");
await admin.getByLabel("Anyone who can open this page may create an account").check();
await admin.getByLabel("Users can generate games from their own configs").check();
await admin.getByRole("button", { name: "Save settings" }).first().click();
await expectText(admin, "Settings saved");
await admin.reload();
await admin.waitForSelector("#view .card");
if (!(await admin.getByLabel("Users can generate games from their own configs").isChecked())) problems.push("setting did not persist");
await shot(admin, "settings");
log("settings survive a reload");
await admin.getByRole("button", { name: "Send a test message" }).click();
await expectText(admin, "Enter a Discord webhook address first");
log("Discord test message without an address explains what is missing");
await admin.locator("details", { hasText: "host.yaml" }).first().locator("summary").click();
await admin.locator("textarea").first().waitFor();
await admin.waitForFunction(() => document.querySelector("details[open] textarea")?.value.includes("server_options"));
log("host.yaml editor loads Archipelago's file");

} catch (error) {
  problems.push(`aborted: ${error.message.split("\n")[0]}`);
  for (const page of pages) {
    await page.screenshot({ path: `${SHOTS}/${page.name}-FAILED.png`, fullPage: true }).catch(() => {});
    console.log(`  [${page.name}] url at failure: ${page.url()}`);
    const toasts = await page.locator(".toast").allInnerTexts().catch(() => []);
    if (toasts.length) console.log(`  [${page.name}] toasts: ${toasts.join(" | ")}`);
  }
}

console.log(`\n4xx responses seen (expected from the negative steps):\n  ${clientErrors.join("\n  ") || "none"}`);
console.log(problems.length ? `\nPROBLEMS (${problems.length}):\n- ${problems.join("\n- ")}` : "\nAll UI flows passed with no browser errors.");
await browser.close();
process.exit(problems.length ? 1 : 0);
