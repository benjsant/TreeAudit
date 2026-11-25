// End-to-end check of the similar images page (not shipped), in the Playwright image:
//   node e2e.cjs <engine> <page url> [stop seconds]   engine: chromium, firefox or firefox-rfp
// With [stop seconds]: "Arrêter" after that long, then start again (resume).
// Clicks "Calculer les signatures" and waits for the end or an error message;
// prints the progress lines it saw, then the final state, as JSON.
const pw = require("/app/node_modules/playwright");

const [engine, url, stopAfter] = process.argv.slice(2);

async function main() {
  const prefs = engine === "firefox-rfp" ? { "privacy.resistFingerprinting": true } : undefined;
  const type = engine.startsWith("firefox") ? "firefox" : engine;
  const browser = await pw[type].launch(prefs ? { firefoxUserPrefs: prefs } : {});
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  await page.goto(url);
  const start = page.locator("[data-start-signing]");
  await start.waitFor({ state: "visible", timeout: 20000 });
  const before = await page.locator("[data-sign-images] .facts").innerText();
  const began = Date.now();
  await start.click();
  let stopped = null;
  if (stopAfter) {
    await page.waitForTimeout(Number(stopAfter) * 1000);
    await page.locator("[data-stop-signing]").click();
    await start.waitFor({ state: "visible", timeout: 60000 });
    stopped = {
      line: await page.locator('[data-field="progress"]').innerText(),
      facts: await page.locator("[data-sign-images] .facts").innerText(),
    };
    await start.click();
  }
  const seen = new Set();
  const finished = page.locator('[data-field="finished"]');
  const error = page.locator('[data-field="error"]');
  for (;;) {
    const line = await page.locator('[data-field="progress"]').innerText();
    if (line) seen.add(line.replace(/encore .*$/, "encore …"));
    if (await finished.isVisible() || await error.isVisible()) break;
    if (Date.now() - began > 600000) break;
    await page.waitForTimeout(1000);
  }
  const result = {
    engine,
    version: browser.version(),
    seconds: (Date.now() - began) / 1000,
    before,
    stopped,
    after: await page.locator("[data-sign-images] .facts").innerText(),
    finished: await finished.isVisible(),
    error: (await error.isVisible()) ? await error.innerText() : null,
    progress: [...seen].slice(0, 6),
    last: await page.locator('[data-field="progress"]').innerText(),
    consoleErrors,
  };
  console.log(JSON.stringify(result, null, 1));
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
