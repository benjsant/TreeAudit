// Visit every page reachable by links (not shipped), in the Playwright image:
//   node tour.cjs <engine> <base url> [max pages]
// Follows same-origin links only (never a button: nothing is changed), at most
// 3 pages per route shape. For each page, desktop light then phone dark:
// HTTP status, console errors, horizontal overflow, requests leaving 127.0.0.1.
// Downloads (/export/...) are fetched, not opened.
const pw = require("/app/node_modules/playwright");

const [engine, base, maxPages = "120"] = process.argv.slice(2);
const origin = new URL(base).origin;

function shape(url) {
  const u = new URL(url);
  return u.pathname.replace(/\/\d+/g, "/N") + ([...u.searchParams.keys()].sort().join(",") ? "?" + [...u.searchParams.keys()].sort().join(",") : "");
}

async function main() {
  const browser = await pw[engine].launch();
  const desktop = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light" });
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });
  const outside = new Set();
  for (const ctx of [desktop, phone]) {
    ctx.on("request", (r) => { if (!r.url().startsWith(origin) && !r.url().startsWith("data:") && !r.url().startsWith("blob:")) outside.add(r.url()); });
  }
  const queue = [base + "/"];
  const seen = new Set();
  const perShape = new Map();
  const report = [];
  const downloads = [];
  while (queue.length && report.length < Number(maxPages)) {
    const url = queue.shift();
    if (seen.has(url)) continue;
    seen.add(url);
    const key = shape(url);
    if ((perShape.get(key) || 0) >= 3) continue;
    perShape.set(key, (perShape.get(key) || 0) + 1);
    if (new URL(url).pathname.startsWith("/export/")) {
      const response = await desktop.request.get(url);
      downloads.push({ url: url.slice(origin.length), status: response.status() });
      continue;
    }
    const row = { url: url.slice(origin.length) };
    for (const [name, ctx] of [["desktop", desktop], ["phone", phone]]) {
      const page = await ctx.newPage();
      const errors = [];
      page.on("console", (m) => m.type() === "error" && !/status of 409/.test(m.text()) && errors.push(m.text()));
      page.on("pageerror", (e) => errors.push(String(e)));
      const response = await page.goto(url, { waitUntil: "load" });
      await page.waitForTimeout(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      row[name] = { status: response.status(), errors, overflow: overflow > 0 ? overflow : 0 };
      if (name === "desktop") {
        for (const href of await page.locator("a[href]").evaluateAll((as) => as.map((a) => a.href))) {
          const u = new URL(href);
          if (u.origin === origin && !u.pathname.startsWith("/api/") && !u.pathname.startsWith("/static/")) {
            u.hash = "";
            if (!seen.has(u.href)) queue.push(u.href);
          }
        }
      }
      await page.close();
    }
    report.push(row);
  }
  const bad = report.filter((r) => ["desktop", "phone"].some((n) => r[n].status >= 400 || r[n].errors.length || r[n].overflow));
  console.log(JSON.stringify({ engine, version: browser.version(), pages: report.length, shapes: perShape.size, downloads, outside: [...outside], bad }, null, 1));
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
