// Ranger and the dates photos were taken (D-071), by clicks only (not shipped):
//   node photo_dates.cjs <base url> <out dir>
// Home -> new analysis -> summary -> Ranger -> "Lire les dates des photos" ->
// back to Ranger -> "par mois". Prints counts only (photo targets by year,
// by month, how many dates were read), never a file name; screenshots of the
// dates panel, desktop light and phone dark. The tidy script is fetched, never run.
const pw = require("/app/node_modules/playwright");

const [base, out] = process.argv.slice(2);

// Photo targets of the tidy script: "Rangé/Photos/<year>/[<year>-<month>/]".
function photoTargets(script) {
  const years = {};
  let byMonth = 0;
  let files = 0;
  for (const line of script.split("\n")) {
    if (!line.startsWith("file ")) continue;
    const found = /(?:^|['\s])Rangé\/Photos\/(\d{4})\/(?:(\d{4})-(\d\d)\/)?/.exec(line);
    if (!found) continue;
    files += 1;
    years[found[1]] = (years[found[1]] || 0) + 1;
    if (found[2]) {
      if (found[2] !== found[1]) throw new Error("month folder of another year");
      byMonth += 1;
    }
  }
  return { files, byMonth, years };
}

async function script(page) {
  const href = await page.getByRole("link", { name: "📥 Script de rangement" }).getAttribute("href");
  return (await page.request.get(new URL(href, page.url()).href)).text();
}

async function main() {
  const browser = await pw.chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const problems = [];
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  const began = Date.now();
  const steps = [];
  const step = (name) => steps.push(`${((Date.now() - began) / 1000).toFixed(1)} s · ${name}`);

  await page.goto(base);
  await page.getByRole("link", { name: "Analyser un dossier" }).click();
  await page.locator("[data-start-scan]").click();
  const result = page.locator('[data-field="result"]');
  await Promise.race([
    page.waitForURL(/\/scans\/\d+$/, { timeout: 600000 }),
    result.waitFor({ state: "visible", timeout: 600000 }),
  ]);
  if (!/\/scans\/\d+$/.test(page.url())) await result.click();
  await page.waitForURL(/\/scans\/\d+$/);
  step("bilan ouvert");

  await page.getByRole("link", { name: "🗂️ Ranger" }).click();
  await page.getByText("Que ranger ?").waitFor();
  const panel = page.locator(".photo-dates");
  const before = (await panel.locator("p").first().innerText()).trim();
  const plain = photoTargets(await script(page));
  step("Ranger ouvert");

  await page.locator("[data-start-photo-dates]").click();
  await page.waitForURL(/\/jobs\/current$/);
  // Back to Ranger by itself once seen running; else through the result link.
  await Promise.race([
    page.waitForURL(/\/organize\?/, { timeout: 1800000 }),
    result.waitFor({ state: "visible", timeout: 1800000 }),
  ]);
  const jobLine = page.url().includes("/organize?")
    ? "(retour automatique)"
    : await page.locator('[data-field="progress"]').innerText();
  if (!page.url().includes("/organize?")) await result.click();
  await page.getByText("Que ranger ?").waitFor();
  step("dates lues, retour sur Ranger");
  const after = (await panel.locator("p").first().innerText()).trim();
  const buttonLeft = await page.locator("[data-start-photo-dates]").count();
  const dated = photoTargets(await script(page));
  const takenReasons = await page.getByText("date de prise de vue", { exact: false }).count();
  await panel.screenshot({ path: `${out}/dates-panel.png` });

  await page.locator('input[name="par_mois"]').check();
  await page.getByRole("button", { name: "🔄 Mettre à jour l'aperçu" }).first().click();
  await page.waitForURL(/par_mois=1/);
  const ticked = await page.locator('input[name="par_mois"]').isChecked();
  const monthly = photoTargets(await script(page));
  step("par mois");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);

  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  phone.on("pageerror", (e) => problems.push(String(e)));
  await phone.goto(page.url());
  await phone.locator(".photo-dates").screenshot({ path: `${out}/dates-panel-phone.png` });
  const phoneOverflow = await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);

  console.log(
    JSON.stringify(
      { steps, before, jobLine, after, buttonLeft, takenReasons, plain, dated, ticked, monthly, overflow, phoneOverflow, problems },
      null,
      1,
    ),
  );
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
