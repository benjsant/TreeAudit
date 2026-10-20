// TreeAudit — minimal JavaScript. File names are untrusted: only textContent is
// used, never innerHTML. State-changing requests carry the CSRF header.
"use strict";

const CSRF_HEADERS = { "Content-Type": "application/json", "X-TreeAudit-Request": "1" };

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: CSRF_HEADERS,
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : "requête refusée");
  }
  return data;
}

function showError(scope, message) {
  const target = scope.querySelector("[data-error]");
  if (target) {
    target.textContent = message;
    target.hidden = false;
  }
}

function humanSize(bytes) {
  const units = ["o", "Kio", "Mio", "Gio", "Tio"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} o` : `${value.toFixed(1)} ${units[unit]}`;
}

// --- starting jobs -------------------------------------------------------------------

function setupStartButtons() {
  for (const button of document.querySelectorAll("[data-start-scan]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await post("/api/scans", { root_id: button.dataset.root, subpath: button.dataset.path });
        window.location.assign("/jobs/current");
      } catch (error) {
        showError(button.closest(".actions"), error.message);
        button.disabled = false;
      }
    });
  }
  for (const button of document.querySelectorAll("[data-start-dupes]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await post(`/api/scans/${encodeURIComponent(button.dataset.scan)}/dupes`, {
          micro: button.dataset.micro === "1",
        });
        window.location.assign("/jobs/current");
      } catch (error) {
        showError(button.closest(".actions"), error.message);
        button.disabled = false;
      }
    });
  }
}

// --- following a job -------------------------------------------------------------------

function setupJobMonitor() {
  const monitor = document.querySelector("[data-job-monitor]");
  if (!monitor) return;
  const field = (name) => monitor.querySelector(`[data-field="${name}"]`);
  const cancel = monitor.querySelector("[data-cancel-job]");

  cancel?.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      await post("/api/jobs/current/cancel", {});
    } catch (error) {
      field("error").textContent = error.message;
      field("error").hidden = false;
    }
  });

  async function refresh() {
    let data;
    try {
      const response = await fetch("/api/jobs/current", { credentials: "same-origin" });
      data = await response.json();
    } catch (error) {
      setTimeout(refresh, 2000);
      return;
    }
    const job = data.job;
    if (!job) return;
    field("label").textContent = job.label;
    field("elapsed").textContent = String(job.elapsed);
    const p = job.progress;
    if (job.kind === "scan") {
      field("progress").textContent =
        `${p.entries} entrées, ${p.errors} erreurs, ${p.exclusions} exclusions`;
      field("current").textContent = p.current_dir || "–";
    } else {
      field("progress").textContent =
        `${p.stage || "préparation"} : ${p.files_hashed}/${p.candidates} fichiers, ` +
        `${humanSize(p.bytes_read)} lus, ${p.errors} erreurs`;
      field("current").textContent = "–";
    }
    if (job.running) {
      field("state").textContent = job.cancel_requested ? "annulation en cours…" : "en cours";
      setTimeout(refresh, 1000);
      return;
    }
    field("state").textContent = job.error ? "échec" : `terminé (${job.status || "?"})`;
    if (cancel) cancel.hidden = true;
    if (job.error) {
      field("error").textContent = job.error;
      field("error").hidden = false;
    }
    const link = field("result");
    if (job.kind === "scan" && job.scan_id) {
      link.href = `/scans/${encodeURIComponent(job.scan_id)}`;
      link.hidden = false;
    } else if (job.kind === "dupes" && job.run_id) {
      link.href = `/dupes/${encodeURIComponent(job.run_id)}`;
      link.hidden = false;
    }
  }
  refresh();
}

// --- selections and proposed commands -----------------------------------------------------

function setupCommandBuilders() {
  for (const builder of document.querySelectorAll("[data-command-builder]")) {
    const picks = () => [...builder.querySelectorAll("[data-pick]")];

    builder.querySelector("[data-select-band]")?.addEventListener("click", (event) => {
      const band = event.currentTarget.dataset.selectBand;
      for (const box of picks()) {
        if (!box.disabled) box.checked = box.dataset.band === band;
      }
    });
    builder.querySelector("[data-select-all-groups]")?.addEventListener("click", () => {
      for (const box of picks()) box.checked = box.dataset.empty !== "1";
    });
    builder.querySelector("[data-select-none]")?.addEventListener("click", () => {
      for (const box of picks()) box.checked = false;
    });

    const output = builder.querySelector("[data-command-output]");
    const text = builder.querySelector("[data-command-text]");
    const warnings = builder.querySelector("[data-warnings]");
    const errorBox = builder.querySelector("[data-error]");

    builder.querySelector("[data-build-command]")?.addEventListener("click", async () => {
      errorBox.hidden = true;
      warnings.replaceChildren();
      const chosen = picks().filter((box) => box.checked).map((box) => Number(box.dataset.pick));
      if (chosen.length === 0) {
        showError(builder, "Coche au moins un élément.");
        return;
      }
      const action = builder.querySelector('input[name="action"]:checked').value;
      const body = { action };
      if (builder.dataset.kind === "dupes") {
        body.groups = chosen;
        body.keep = {};
        for (const number of chosen) {
          const radio = builder.querySelector(`input[name="keep-${number}"]:checked`);
          if (radio && !radio.hasAttribute("data-default")) body.keep[number] = Number(radio.value);
        }
      } else {
        body.numbers = chosen;
        body.min_confidence = Number(builder.dataset.minConfidence || 30);
      }
      try {
        const data = await post(builder.dataset.endpoint, body);
        const notes = [];
        if (action === "delete") notes.push("⚠ Suppression DÉFINITIVE : préfère la corbeille si tu as un doute.");
        if (action === "archive") notes.push("⚠ Une archive sur le même disque que l'original n'est pas une vraie sauvegarde.");
        if (data.outside_green && data.outside_green.length) {
          notes.push(`⚠ Sélection hors du vert (${data.outside_green.join(", ")}) : vérifie ces éléments.`);
        }
        for (const path of data.skipped || []) notes.push(`⚠ Ignoré (nom non affichable sans risque) : ${path}`);
        for (const note of notes) {
          const item = document.createElement("li");
          item.textContent = note;
          warnings.append(item);
        }
        text.textContent = data.command || "(aucune commande possible pour cette sélection)";
        output.hidden = false;
      } catch (error) {
        showError(builder, error.message);
      }
    });

    builder.querySelector("[data-copy]")?.addEventListener("click", async () => {
      const status = builder.querySelector("[data-copy-status]");
      try {
        await navigator.clipboard.writeText(text.textContent);
        status.textContent = "copié";
      } catch (error) {
        status.textContent = "copie impossible : sélectionne le texte à la main";
      }
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  setupStartButtons();
  setupJobMonitor();
  setupCommandBuilders();
});
