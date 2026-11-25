// Tidying only development and software, by clicks (not shipped):
//   node ranger.cjs <summary url> <out dir>
// Summary -> "Ranger" -> "Seulement le développement et les logiciels" ->
// reads the totals and what stays, fetches the tidy script (never runs it),
// takes screenshots: desktop light, phone dark.
const pw = require("/app/node_modules/playwright");

const [summary, out] = process.argv.slice(2);

async function main() {
  const browser = await pw.chromium.launch();
  const problems = [];
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
  page.on("pageerror", (e) => problems.push(String(e)));
  await page.goto(summary);
  await page.getByRole("link", { name: "🗂️ Ranger" }).click();
  await page.getByText("Que ranger ?").waitFor();
  const ticked = await page.locator('input[name="ranger"]:checked').evaluateAll((boxes) => boxes.map((b) => b.value));
  await page.getByRole("link", { name: "💻 Seulement le développement et les logiciels" }).click();
  await page.getByText("Laissés en place, catégories non cochées").waitFor();
  const after = await page.locator('input[name="ranger"]:checked').evaluateAll((boxes) => boxes.map((b) => b.value));
  const totals = await page.locator(".organize-totals tr").allInnerTexts();
  const left = await page.getByText("Laissés en place, catégories non cochées").innerText();
  const href = await page.getByRole("link", { name: "📥 Script de rangement" }).getAttribute("href");
  const script = await (await page.request.get(new URL(href, page.url()).href)).text();
  const moves = script.split("\n").filter((line) => /^(file|folder) /.test(line)).length;
  await page.locator(".panel").first().screenshot({ path: `${out}/ranger.png` });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);

  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  await phone.goto(page.url());
  await phone.locator(".panel").first().screenshot({ path: `${out}/ranger-phone.png` });
  const phoneOverflow = await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  console.log(JSON.stringify({ ticked, after, totals, left, moves, scriptLines: script.split("\n").length, overflow, phoneOverflow, problems }, null, 1));
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
