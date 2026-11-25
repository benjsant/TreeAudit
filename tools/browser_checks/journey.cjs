// A user's whole journey to the similar images, by clicks only (not shipped):
//   node journey.cjs <base url> <root label> <out dir>
// Home -> new analysis -> the root -> analyse -> summary -> similar images ->
// compute the signatures -> groups -> tick the first group, keep its second
// image, prepare the trash command (never run). Screenshots of the first groups.
const pw = require("/app/node_modules/playwright");

const [base, rootLabel, out] = process.argv.slice(2);

async function main() {
  const browser = await pw.chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const problems = [];
  page.on("console", (m) => m.type() === "error" && !m.text().includes("409") && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  const steps = [];
  const step = (name) => steps.push(`${((Date.now() - began) / 1000).toFixed(1)} s · ${name}`);
  const began = Date.now();

  await page.goto(base);
  await page.getByRole("link", { name: "Analyser un dossier" }).click();
  // With a single root, the picker opens it at once.
  if (!(await page.locator("[data-start-scan]").count())) {
    await page.getByRole("link", { name: new RegExp(rootLabel) }).click();
  }
  step("dossier choisi");
  await page.locator("[data-start-scan]").click();
  // A finished analysis opens its summary by itself.
  await page.waitForURL(/\/scans\/\d+$/, { timeout: 600000 });
  step(`bilan ouvert : ${(await page.locator("p.lead").first().innerText()).trim()}`);
  await page.getByRole("link", { name: "🖼️ Photos ressemblantes" }).click();
  await page.locator("[data-start-signing]").waitFor({ state: "visible" });
  const before = await page.locator("[data-sign-images] .facts").innerText();
  await page.locator("[data-start-signing]").click();
  step("calcul lancé");
  let last = "";
  for (;;) {
    const lead = page.locator("#groups-title + p.lead");
    if (await lead.count()) break;
    const error = page.locator('[data-field="error"]');
    if (await error.isVisible()) throw new Error(await error.innerText());
    const line = await page.locator('[data-field="progress"]').innerText().catch(() => "");
    if (line && line !== last) last = line;
    if (Date.now() - began > 1800000) throw new Error(`trop long, dernier état : ${last}`);
    await page.waitForTimeout(2000);
  }
  step(`signatures calculées (dernier état vu : ${last.replace(/ · encore.*$/, "")})`);
  const facts = await page.locator("[data-sign-images] .facts").innerText();
  const lead = await page.locator("#groups-title + p.lead").innerText();
  const groups = await page.locator("[data-pick]").count();
  for (let i = 0; i < Math.min(groups, 8); i++) {
    const item = page.locator(".items .item").nth(i);
    await item.scrollIntoViewIfNeeded();
    await item.locator("img").last().scrollIntoViewIfNeeded();
    await page.waitForTimeout(600);
    await item.screenshot({ path: `${out}/group-${i + 1}.png` });
  }
  await page.locator('[data-pick="1"]').check();
  await page.locator('input[name="keep-1"][value="2"]').check();
  await page.locator("[data-build-command]").click();
  await page.locator("[data-command-output]").waitFor({ state: "visible" });
  const command = await page.locator("[data-command-text]").textContent();
  step("commande préparée (non lancée)");
  console.log(JSON.stringify({ steps, before, facts, lead, groups, command, problems }, null, 1));
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
