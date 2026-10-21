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
  const target = scope?.querySelector("[data-error]");
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
        showError(button.closest(".actions, .choice"), error.message);
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
        showError(button.closest(".actions, .choice"), error.message);
        button.disabled = false;
      }
    });
  }
}

// --- archives ------------------------------------------------------------------------------

function setupArchiveButtons() {
  for (const button of document.querySelectorAll("[data-start-archive]")) {
    button.addEventListener("click", async () => {
      const question =
        `Créer une archive vérifiée de « ${button.dataset.name} » dans ${button.dataset.where} ?\n\n` +
        "Le dossier d'origine ne sera pas modifié.";
      if (!window.confirm(question)) return;
      button.disabled = true;
      const body = button.dataset.root
        ? { root_id: button.dataset.root, subpath: button.dataset.path }
        : {};
      try {
        await post(button.dataset.endpoint, body);
        window.location.assign("/jobs/current");
      } catch (error) {
        showError(button.closest(".actions, .choice"), error.message);
        button.disabled = false;
      }
    });
  }
}

const ARCHIVE_STAGES = {
  preparing: "Préparation",
  writing: "Écriture",
  verifying: "Vérification",
  done: "Terminé",
};

function archiveProgress(p) {
  const stage = ARCHIVE_STAGES[p.stage] || p.stage;
  if (p.stage === "preparing") {
    return `${stage} : ${p.entries_total.toLocaleString("fr-FR")} éléments trouvés`;
  }
  if (p.stage === "verifying") {
    return `${stage} : ${humanSize(p.bytes_verified)} relus sur ${humanSize(p.bytes_read)}`;
  }
  return (
    `${stage} : ${p.entries_done.toLocaleString("fr-FR")} / ` +
    `${p.entries_total.toLocaleString("fr-FR")} éléments ` +
    `(${humanSize(p.bytes_read)} / ${humanSize(p.bytes_total)})`
  );
}

const ARCHIVE_TITLES = {
  complete: "✔ Archive complète et vérifiée",
  incomplete: "⚠ Archive INCOMPLÈTE (vérifiée, mais il manque des éléments)",
  cancelled: "Archivage annulé : rien n'a été conservé",
  failed: "✘ Archivage échoué : rien n'a été conservé",
};

function showArchiveResult(job) {
  const box = document.querySelector("[data-archive-result]");
  if (!box || !job.archive) return;
  const field = (name) => box.querySelector(`[data-field="${name}"]`);
  const result = job.archive;
  field("archive-title").textContent = ARCHIVE_TITLES[job.status] || job.status;
  field("archive-path").textContent = result.path || "";
  const details = [];
  if (result.verified) {
    details.push(
      `${result.entries.toLocaleString("fr-FR")} éléments, ${humanSize(result.bytes_read)} lus, ` +
        `archive de ${humanSize(result.archive_size)}`,
    );
  }
  if (result.failure) details.push(`Cause : ${result.failure}`);
  field("archive-details").textContent = details.join(" · ");
  const advice = field("archive-advice");
  advice.hidden = job.status !== "incomplete";
  advice.textContent = "Ne supprime pas l'original en te fiant à cette archive : regarde d'abord ce qui manque.";
  field("archive-same-disk").hidden = !(result.same_disk && result.path);
  const problems = field("archive-problems");
  problems.replaceChildren();
  for (const problem of result.problems) {
    const item = document.createElement("li");
    item.textContent = `${problem.path} : ${problem.message}`;
    problems.append(item);
  }
  const hidden = result.problem_count - result.problems.length;
  if (hidden > 0) {
    const item = document.createElement("li");
    item.textContent = `… et ${hidden.toLocaleString("fr-FR")} autres`;
    problems.append(item);
  }
  field("archive-problems-title").textContent = `Éléments manquants ou incertains (${result.problem_count})`;
  field("archive-problems-box").hidden = result.problem_count === 0;
  field("archive-problems-box").open = job.status === "incomplete";
  box.hidden = false;
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

  let wasRunning = false;

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
      field("progress").textContent = `${p.entries.toLocaleString("fr-FR")} éléments vus`;
      field("current").textContent = p.current_dir || "–";
    } else if (job.kind === "archive") {
      field("progress").textContent = archiveProgress(p);
      field("current").textContent = p.problems ? `${p.problems} élément(s) à signaler` : "";
    } else {
      field("progress").textContent =
        `${p.files_hashed.toLocaleString("fr-FR")} / ${p.candidates.toLocaleString("fr-FR")} ` +
        `fichiers comparés (${humanSize(p.bytes_read)} lus)`;
      field("current").textContent = "–";
    }
    if (job.running) {
      wasRunning = true;
      field("state").textContent = job.cancel_requested ? "annulation en cours…" : "en cours";
      setTimeout(refresh, 1000);
      return;
    }
    field("state").textContent = job.error ? "échec" : "terminé";
    if (cancel) cancel.hidden = true;
    if (job.error) {
      field("error").textContent = job.error;
      field("error").hidden = false;
    }
    if (job.kind === "archive") {
      showArchiveResult(job);
      return;
    }
    const link = field("result");
    let target = null;
    if (job.kind === "scan" && job.scan_id) {
      target = `/scans/${encodeURIComponent(job.scan_id)}`;
    } else if (job.kind === "dupes" && job.run_id) {
      target = `/dupes/${encodeURIComponent(job.run_id)}`;
    }
    if (target) {
      link.href = target;
      link.hidden = false;
      // A finished job goes straight to its result; an error stays on screen.
      if (!job.error && wasRunning) window.location.assign(target);
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
  setupArchiveButtons();
  setupJobMonitor();
  setupCommandBuilders();
});
