// TreeAudit — similar images (D-069): the browser decodes each image, this
// script reduces it itself and sends back a 256-bit gradient hash and a 32 x 32
// grey thumbnail. The same bits in Chromium, Firefox and WebKit, because the
// browser never does the reduction (tools/similar_images_proto/). Loaded after
// app.js: post() and humanSize() come from there.
"use strict";

const SIGNATURE = {
  algo: 1, // db/images.py ALGO
  cellsWide: 17,
  cellsHigh: 16,
  thumb: 32,
  parallel: 4, // measured: 4 at once is about twice as fast, 8 barely more
  batch: 50, // results sent together (the server takes at most 500)
};
const DECODE = { imageOrientation: "from-image", colorSpaceConversion: "none", premultiplyAlpha: "none" };

// An 8 x 8 lossless PNG whose pixels are known (tools/similar_images_proto/make_selftest.py).
const SELF_TEST_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAA00lEQVR42gHIADf/AAAAKB8NKD4aKF0nKHw0KJtBKLpOKNlbKAAHHSgm" +
  "Ki1FNzJkRDeDUTyiXkHBa0bgeEsADjooLUcyTFQ8a2FGim5QqXtayIhk55VuABVXKDRkN1NxRnJ+VZGLZLCYc8+lgu6ykQAcdCg7gTxa" +
  "jlB5m2SYqHi3tYzWwqD1z7QAI5EoQp5BYatagLhzn8WMvtKl3d++/OzXACquKEm7RmjIZIfVgqbioMXvvuT83AMJ+gAxyyhQ2Etv5W6O" +
  "8pGt/7TMDNfrGfoKJh0uC1hRpbbCJgAAAABJRU5ErkJggg==";

function selfTestPixel(x, y) {
  return [(x * 31 + y * 7) % 256, (x * 13 + y * 29) % 256, (x * y * 5 + 40) % 256];
}

function luma(data, i) {
  return 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
}

function pixelsOf(bitmap) {
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(bitmap, 0, 0);
  return context.getImageData(0, 0, bitmap.width, bitmap.height).data;
}

// Mean grey of each of wide x high cells: every pixel goes to the cell its centre falls in.
function cells(data, width, height, wide, high) {
  const sum = new Float64Array(wide * high);
  const count = new Uint32Array(wide * high);
  const column = new Uint16Array(width);
  const row = new Uint16Array(height);
  for (let x = 0; x < width; x++) column[x] = Math.min(wide - 1, Math.floor(((x + 0.5) * wide) / width));
  for (let y = 0; y < height; y++) row[y] = Math.min(high - 1, Math.floor(((y + 0.5) * high) / height));
  for (let y = 0; y < height; y++) {
    const line = row[y] * wide;
    const base = y * width * 4;
    for (let x = 0; x < width; x++) {
      const k = line + column[x];
      sum[k] += luma(data, base + x * 4);
      count[k]++;
    }
  }
  for (let k = 0; k < sum.length; k++) sum[k] /= count[k] || 1;
  return sum;
}

// One bit per pair of neighbouring cells of a row: is the right one lighter?
function gradientHash(grey) {
  const { cellsWide: wide, cellsHigh: high } = SIGNATURE;
  let hex = "";
  let nibble = 0;
  let bits = 0;
  for (let y = 0; y < high; y++) {
    for (let x = 0; x < wide - 1; x++) {
      nibble = (nibble << 1) | (grey[y * wide + x] < grey[y * wide + x + 1] ? 1 : 0);
      if (++bits === 4) {
        hex += nibble.toString(16);
        nibble = 0;
        bits = 0;
      }
    }
  }
  return hex;
}

async function signatureOf(blob) {
  const bitmap = await createImageBitmap(blob, DECODE);
  const { width, height } = bitmap;
  let data;
  try {
    data = pixelsOf(bitmap);
  } finally {
    bitmap.close();
  }
  const hash = gradientHash(cells(data, width, height, SIGNATURE.cellsWide, SIGNATURE.cellsHigh));
  let thumb = "";
  for (const value of cells(data, width, height, SIGNATURE.thumb, SIGNATURE.thumb)) {
    thumb += Math.round(value).toString(16).padStart(2, "0");
  }
  return { hash, thumb, width, height };
}

// How many pixels of the known image come back wrong: a browser that adds noise
// to what a page reads (anti-fingerprinting) would give random signatures.
async function selfTest() {
  const bytes = Uint8Array.from(atob(SELF_TEST_PNG), (c) => c.charCodeAt(0));
  const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }), DECODE);
  const data = pixelsOf(bitmap);
  bitmap.close();
  let wrong = 0;
  for (let y = 0; y < 8; y++) {
    for (let x = 0; x < 8; x++) {
      const i = (y * 8 + x) * 4;
      const [r, g, b] = selfTestPixel(x, y);
      if (data[i] !== r || data[i + 1] !== g || data[i + 2] !== b) wrong++;
    }
  }
  return wrong;
}

function setupImageSignatures() {
  const panel = document.querySelector("[data-sign-images]");
  if (!panel) return;
  const scan = panel.dataset.scan;
  const field = (name) => panel.querySelector(`[data-field="${name}"]`);
  const start = panel.querySelector("[data-start-signing]");
  const stop = panel.querySelector("[data-stop-signing]");
  let stopping = false;

  const show = (state) => {
    field("total").textContent = state.total.toLocaleString("fr-FR");
    field("done").textContent = state.done.toLocaleString("fr-FR");
    field("failed").textContent = state.failed.toLocaleString("fr-FR");
    field("todo").textContent = state.todo.length.toLocaleString("fr-FR");
    start.hidden = state.todo.length === 0;
    field("finished").hidden = state.todo.length > 0 || state.total === 0;
    field("none").hidden = state.total > 0;
  };
  const fail = (message) => {
    const error = field("error");
    error.textContent = message;
    error.hidden = false;
  };

  const refresh = async () => {
    try {
      show(await post(`/api/scans/${scan}/images/todo`, {}));
    } catch (error) {
      fail(error.message);
    }
  };

  start.addEventListener("click", async () => {
    field("error").hidden = true;
    start.disabled = true;
    start.hidden = true; // shown again by refresh() if images are left
    stopping = false;
    try {
      if ((await selfTest()) !== 0) {
        fail(
          "Ton navigateur brouille les images que lit une page (protection contre le pistage : " +
            "Brave, LibreWolf, Tor Browser, Firefox en protection renforcée…). Les ressemblances " +
            "seraient fausses : ouvre TreeAudit dans un autre navigateur, ou désactive cette " +
            "protection pour 127.0.0.1.",
        );
        return;
      }
      const state = await post(`/api/scans/${scan}/images/todo`, {});
      if (state.algo !== SIGNATURE.algo) {
        fail("La page n'est pas à jour : recharge-la.");
        return;
      }
      await signAll(scan, state, field, () => stopping, stop);
    } catch (error) {
      fail(`Calcul interrompu : ${error.message}`);
    } finally {
      start.disabled = false;
      stop.hidden = true;
      await refresh();
    }
  });
  stop.addEventListener("click", () => {
    stopping = true;
    stop.disabled = true;
    field("progress").textContent = "Arrêt après les images en cours…";
  });
  refresh();
}

async function signAll(scan, state, field, stopped, stop) {
  const todo = state.todo;
  const results = [];
  let next = 0;
  let finished = 0;
  let bytes = 0;
  const began = performance.now();
  stop.hidden = false;
  stop.disabled = false;

  const flush = async () => {
    while (results.length) {
      const items = results.splice(0, SIGNATURE.batch);
      await post(`/api/scans/${scan}/images/signatures`, { algo: SIGNATURE.algo, items });
    }
  };
  const report = () => {
    const seconds = (performance.now() - began) / 1000;
    const rate = finished / Math.max(seconds, 0.001);
    const left = rate > 0 ? Math.ceil((todo.length - finished) / rate) : 0;
    field("progress").textContent =
      `${finished.toLocaleString("fr-FR")} / ${todo.length.toLocaleString("fr-FR")} images · ` +
      `${humanSize(bytes)} lus · encore ${left < 90 ? `${left} s` : `${Math.round(left / 60)} min`}`;
  };

  async function worker() {
    while (next < todo.length && !stopped()) {
      const entry = todo[next++];
      let item;
      try {
        const response = await fetch(`/api/scans/${scan}/entries/${entry}/image`, { credentials: "same-origin" });
        if (!response.ok) {
          item = { entry, failure: "unreadable" };
        } else {
          const blob = await response.blob();
          bytes += blob.size;
          try {
            item = { entry, ...(await signatureOf(blob)) };
          } catch (error) {
            item = { entry, failure: "undecodable" };
          }
        }
      } catch (error) {
        throw new Error("le serveur ne répond plus");
      }
      results.push(item);
      finished++;
      if (results.length >= SIGNATURE.batch) await flush();
    }
  }

  const timer = setInterval(report, 500);
  try {
    await Promise.all(Array.from({ length: SIGNATURE.parallel }, worker));
  } finally {
    clearInterval(timer);
    report();
    await flush();
  }
  field("progress").textContent = stopped()
    ? `Arrêté : ${finished.toLocaleString("fr-FR")} image(s) traitée(s), la suite reprendra là.`
    : `Terminé : ${finished.toLocaleString("fr-FR")} image(s) traitée(s).`;
}

document.addEventListener("DOMContentLoaded", setupImageSignatures);
