// Prototype harness (not shipped), run in the Playwright image without network:
//   node run.cjs hash <engine> <method> <dir...>   hashes -> /work/out/<engine>-<dir>.json (PAR=n: n at once)
//   node run.cjs gen <engine> <sources.txt>        variants of each source -> /work/variants/
//   node run.cjs sheet <engine> <pairs.json>       contact sheets -> /work/out/sheet-N.jpg
// /work holds hash.js and selftest.json; images are served to the page by request interception.
const pw = require("/app/node_modules/playwright");
const fs = require("fs");
const path = require("path");

const [mode, engine = "chromium", arg = "own", ...dirs] = process.argv.slice(2);
const ORIGIN = "http://proto.local";
const TYPES = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp" };
const fileUrl = (f) => `${ORIGIN}/file${encodeURI(f)}`;

function listImages(dir) {
  const found = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...listImages(full));
    else if (entry.isFile() && TYPES[path.extname(entry.name).toLowerCase()]) found.push(full);
  }
  return found.sort();
}

async function main() {
  const prefs = engine === "firefox-rfp" ? { "privacy.resistFingerprinting": true } : undefined;
  const type = engine.startsWith("firefox") ? "firefox" : engine;
  const browser = await pw[type].launch(prefs ? { firefoxUserPrefs: prefs } : {});
  const page = await browser.newPage();
  await page.route(`${ORIGIN}/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: "<!doctype html><title>proto</title>" });
    const file = decodeURIComponent(url.pathname.slice("/file".length));
    return route.fulfill({ contentType: TYPES[path.extname(file).toLowerCase()] || "application/octet-stream", body: fs.readFileSync(file) });
  });
  await page.goto(`${ORIGIN}/`);
  await page.addScriptTag({ content: fs.readFileSync("/work/hash.js", "utf8") });
  const self = JSON.parse(fs.readFileSync("/work/selftest.json", "utf8"));
  const wrong = await page.evaluate(([b64, rgb]) => selfTest(b64, rgb), [self.png, self.rgb]);

  if (mode === "gen") {
    // Variants of each source listed in <arg>: written to /work/variants/<name>__<kind>.
    const sources = fs.readFileSync(arg, "utf8").split("\n").filter(Boolean);
    for (const src of sources) {
      const variants = await page.evaluate(async (url) => {
        const bmp = await createImageBitmap(await (await fetch(url)).blob(), { imageOrientation: "from-image" });
        const W = bmp.width, H = bmp.height;
        async function make(w, h, draw, type = "image/jpeg", quality = 0.9) {
          const c = new OffscreenCanvas(w, h), ctx = c.getContext("2d");
          draw(ctx);
          const buf = new Uint8Array(await (await c.convertToBlob({ type, quality })).arrayBuffer());
          let s = ""; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
          return btoa(s);
        }
        const cx = Math.round(W * 0.05), cy = Math.round(H * 0.05);
        return {
          "r50.jpg": await make(W >> 1, H >> 1, (c) => c.drawImage(bmp, 0, 0, W >> 1, H >> 1), "image/jpeg", 0.92),
          "r25.jpg": await make(W >> 2, H >> 2, (c) => c.drawImage(bmp, 0, 0, W >> 2, H >> 2), "image/jpeg", 0.85),
          "q40.jpg": await make(W, H, (c) => c.drawImage(bmp, 0, 0), "image/jpeg", 0.4),
          "conv.webp": await make(W, H, (c) => c.drawImage(bmp, 0, 0), "image/webp", 0.8),
          "crop5.jpg": await make(W - 2 * cx, H - 2 * cy, (c) => c.drawImage(bmp, -cx, -cy)),
          "bright.jpg": await make(W, H, (c) => { c.filter = "brightness(1.15)"; c.drawImage(bmp, 0, 0); }),
          "grey.jpg": await make(W, H, (c) => { c.filter = "grayscale(1)"; c.drawImage(bmp, 0, 0); }),
          "mark.jpg": await make(W, H, (c) => {
            c.drawImage(bmp, 0, 0); c.font = `${Math.round(H / 12)}px sans-serif`;
            c.fillStyle = "rgba(255,255,255,0.8)"; c.fillText("© 2024 vacances", W * 0.55, H * 0.92);
          }),
          "rot90.jpg": await make(H, W, (c) => { c.translate(H, 0); c.rotate(Math.PI / 2); c.drawImage(bmp, 0, 0); }),
          "mirror.jpg": await make(W, H, (c) => { c.translate(W, 0); c.scale(-1, 1); c.drawImage(bmp, 0, 0); }),
        };
      }, fileUrl(src));
      const base = path.basename(src).replace(/\.[^.]+$/, "");
      for (const [kind, b64] of Object.entries(variants)) {
        fs.writeFileSync(`/work/variants/${base}__${kind}`, Buffer.from(b64, "base64"));
      }
    }
    console.log(JSON.stringify({ engine, selftest_wrong: wrong, sources: sources.length }));
  } else if (mode === "sheet") {
    const pairs = JSON.parse(fs.readFileSync(arg, "utf8"));
    for (let i = 0; i < pairs.length; i += 6) {
      const chunk = pairs.slice(i, i + 6).map(([a, b, label]) => [fileUrl(a), fileUrl(b), label]);
      const b64 = await page.evaluate(([p]) => sheet(p, 420, 315), [chunk]);
      fs.writeFileSync(`/work/out/sheet-${i / 6 + 1}.jpg`, Buffer.from(b64, "base64"));
    }
    console.log(JSON.stringify({ engine, sheets: Math.ceil(pairs.length / 6) }));
  } else {
    const files = dirs.flatMap(listImages);
    const urls = files.map(fileUrl);
    const par = Number(process.env.PAR || 1);
    const t0 = Date.now();
    const rows = par > 1
      ? await page.evaluate(([u, m, n]) => hashAllParallel(u, m, n), [urls, arg, par])
      : await page.evaluate(([u, m]) => hashAll(u, m), [urls, arg.split(",")]);
    for (const row of rows) row.file = decodeURI(new URL(row.url).pathname.slice("/file".length)), delete row.url;
    const result = { engine, version: browser.version(), selftest_wrong: wrong, seconds: (Date.now() - t0) / 1000, rows };
    fs.writeFileSync(`/work/out/${engine}-${path.basename(dirs[0])}.json`, JSON.stringify(result));
    console.log(JSON.stringify({ engine, version: result.version, selftest_wrong: wrong, images: rows.length, seconds: result.seconds }));
  }
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
