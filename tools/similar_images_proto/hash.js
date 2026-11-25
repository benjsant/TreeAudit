// Prototype (not shipped): perceptual hash (gradient, 16x16 = 256 bits) computed in the browser.
// The browser only decodes; the reduction to 17x16 grey cells is done here
// ("own"), by the browser ("browser"), or by the browser to 512 px then here ("mixed").
// Only "own" gives the same bits in Chromium, Firefox and WebKit.

const DECODE = { imageOrientation: "from-image", colorSpaceConversion: "none", premultiplyAlpha: "none" };
const OW = 17, OH = 16;

function luma(d, i) {
  return 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
}

function pixelsOf(bitmap) {
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bitmap, 0, 0);
  return ctx.getImageData(0, 0, bitmap.width, bitmap.height).data;
}

// Mean grey of each of the OW x OH cells: every source pixel goes to the cell its centre falls in.
function cells(data, w, h, OW = 17, OH = 16) {
  const sum = new Float64Array(OW * OH), count = new Uint32Array(OW * OH);
  const cx = new Uint16Array(w), cy = new Uint16Array(h);
  for (let x = 0; x < w; x++) cx[x] = Math.min(OW - 1, Math.floor(((x + 0.5) * OW) / w));
  for (let y = 0; y < h; y++) cy[y] = Math.min(OH - 1, Math.floor(((y + 0.5) * OH) / h));
  for (let y = 0; y < h; y++) {
    const row = cy[y] * OW, base = y * w * 4;
    for (let x = 0; x < w; x++) {
      const k = row + cx[x];
      sum[k] += luma(data, base + x * 4);
      count[k]++;
    }
  }
  for (let k = 0; k < sum.length; k++) sum[k] /= count[k] || 1;
  return sum;
}

function gradientHash(grey) {
  let hex = "", nibble = 0, bits = 0;
  for (let y = 0; y < OH; y++) {
    for (let x = 0; x < OW - 1; x++) {
      nibble = (nibble << 1) | (grey[y * OW + x] < grey[y * OW + x + 1] ? 1 : 0);
      if (++bits === 4) { hex += nibble.toString(16); nibble = 0; bits = 0; }
    }
  }
  return hex;
}

async function hashBlob(blob, method) {
  if (method === "browser") {
    const bmp = await createImageBitmap(blob, { ...DECODE, resizeWidth: OW, resizeHeight: OH, resizeQuality: "high" });
    const data = pixelsOf(bmp); bmp.close();
    const grey = new Float64Array(OW * OH);
    for (let k = 0; k < grey.length; k++) grey[k] = luma(data, k * 4);
    return gradientHash(grey);
  }
  let bmp = await createImageBitmap(blob, DECODE);
  if (method === "mixed") {
    const scale = Math.min(1, 512 / Math.max(bmp.width, bmp.height));
    const w = Math.max(OW, Math.round(bmp.width * scale)), h = Math.max(OH, Math.round(bmp.height * scale));
    bmp.close();
    bmp = await createImageBitmap(blob, { ...DECODE, resizeWidth: w, resizeHeight: h, resizeQuality: "high" });
  }
  const { width, height } = bmp;
  const data = pixelsOf(bmp); bmp.close();
  const hash = gradientHash(cells(data, width, height));
  if (method !== "own+thumb") return hash;
  // A finer grey thumbnail, to confirm a candidate pair: 32 x 32 bytes, as hex.
  const fine = cells(data, width, height, 32, 32);
  let hex = "";
  for (const v of fine) hex += Math.round(v).toString(16).padStart(2, "0");
  return `${hash}:${hex}`;
}

// The page reads back exactly the pixels of a known lossless image, or refuses.
async function selfTest(pngBase64, expected) {
  const bytes = Uint8Array.from(atob(pngBase64), (c) => c.charCodeAt(0));
  const bmp = await createImageBitmap(new Blob([bytes], { type: "image/png" }), DECODE);
  const data = pixelsOf(bmp); bmp.close();
  let wrong = 0;
  for (let i = 0, j = 0; i < data.length; i += 4, j += 3) {
    if (data[i] !== expected[j] || data[i + 1] !== expected[j + 1] || data[i + 2] !== expected[j + 2]) wrong++;
  }
  return wrong;
}

async function hashAll(urls, methods) {
  const out = [];
  for (const url of urls) {
    const blob = await (await fetch(url)).blob();
    const row = { url, bytes: blob.size };
    for (const m of methods) {
      const t0 = performance.now();
      try { row[m] = await hashBlob(blob, m); } catch (e) { row[m] = "ERR " + e.message; }
      row[m + "_ms"] = Math.round((performance.now() - t0) * 10) / 10;
    }
    out.push(row);
  }
  return out;
}

// Several images at once: decoding runs off the page's main thread in browsers.
async function hashAllParallel(urls, method, workers) {
  const out = new Array(urls.length);
  let next = 0;
  async function worker() {
    while (next < urls.length) {
      const i = next++;
      const blob = await (await fetch(urls[i])).blob();
      out[i] = { url: urls[i], bytes: blob.size };
      try { out[i][method] = await hashBlob(blob, method); } catch (e) { out[i][method] = "ERR " + e.message; }
    }
  }
  await Promise.all(Array.from({ length: workers }, worker));
  return out;
}

// A contact sheet: each pair side by side, scaled to fit, with its distance.
async function sheet(pairs, cellW, cellH) {
  const canvas = new OffscreenCanvas(cellW * 2 + 30, (cellH + 28) * pairs.length);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.font = "16px sans-serif"; ctx.fillStyle = "#000";
  for (let i = 0; i < pairs.length; i++) {
    const [a, b, label] = pairs[i];
    const y = i * (cellH + 28);
    ctx.fillText(label, 6, y + 18);
    for (const [k, url] of [[0, a], [1, b]]) {
      const bmp = await createImageBitmap(await (await fetch(url)).blob(), { imageOrientation: "from-image" });
      const s = Math.min(cellW / bmp.width, cellH / bmp.height);
      ctx.drawImage(bmp, k * (cellW + 30), y + 24, bmp.width * s, bmp.height * s);
      bmp.close();
    }
  }
  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.85 });
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = ""; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(s);
}
