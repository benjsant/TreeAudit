// Look at the groups of the similar images page (not shipped), in the Playwright image:
//   node groups.cjs <page url> <out dir>
// Ticks the first group, keeps its second image instead, prepares the trash
// command, and takes screenshots: desktop light, phone dark.
const pw = require("/app/node_modules/playwright");

const [url, out] = process.argv.slice(2);

async function main() {
  const browser = await pw.chromium.launch();
  const problems = [];
  const desktop = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  desktop.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  desktop.on("pageerror", (e) => problems.push(String(e)));
  await desktop.goto(url);
  await desktop.waitForLoadState("networkidle");
  const lead = await desktop.locator("#groups-title + p.lead").innerText();
  const groups = await desktop.locator("[data-pick]").count();
  await desktop.locator('[data-pick="1"]').check();
  await desktop.locator('input[name="keep-1"][value="2"]').check();
  const captions = await desktop.locator('input[name="keep-1"] + span').allInnerTexts();
  await desktop.locator("[data-build-command]").click();
  await desktop.locator("[data-command-output]").waitFor({ state: "visible" });
  const command = await desktop.locator("[data-command-text]").textContent();
  const warnings = await desktop.locator("[data-warnings] li").allInnerTexts();
  await desktop.locator("#groups-title").scrollIntoViewIfNeeded();
  await desktop.screenshot({ path: `${out}/desktop.png` });
  await desktop.locator(".items .item").nth(1).screenshot({ path: `${out}/group.png` });
  const overflow = await desktop.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);

  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  await phone.goto(url);
  await phone.waitForLoadState("networkidle");
  await phone.locator("#groups-title").scrollIntoViewIfNeeded();
  await phone.screenshot({ path: `${out}/phone.png` });
  await phone.locator(".items .item").nth(1).screenshot({ path: `${out}/phone-group.png` });
  const phoneOverflow = await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  console.log(JSON.stringify({ lead, groups, captions, command, warnings, overflow, phoneOverflow, problems }, null, 1));
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
