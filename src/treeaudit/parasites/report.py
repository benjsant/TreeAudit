"""Text rendering of parasite findings and of the proposed commands (French UI)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from treeaudit.parasites.commands import Action, cleanup_command, recreate_command
from treeaudit.parasites.detect import Band, Finding

BAND_LABELS = {
    Band.GREEN: "🟢 vert",
    Band.ORANGE: "🟠 orange",
    Band.RED: "🔴 rouge",
}
BAND_MEANING = {
    Band.GREEN: "recréable, suppression a priori sans risque",
    Band.ORANGE: "probable, à vérifier",
    Band.RED: "doute, ne pas supprimer sans regarder",
}
ACTION_LABELS = {
    Action.TRASH: "mise à la corbeille (réversible)",
    Action.DELETE: "suppression DÉFINITIVE",
    Action.ARCHIVE: "archivage .tar.gz (l'original n'est pas touché)",
}


class SelectionError(ValueError):
    """The --select value is invalid."""


def human_size(size: int | None) -> str:
    if size is None:
        return "taille inconnue"
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if value < 1024 or unit == "Tio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def parse_selection(text: str, findings: Sequence[Finding]) -> list[Finding]:
    """Accept numbers, ranges and band names: "1,3,5-7", "green", "green,12"."""
    by_number = {f.number: f for f in findings}
    bands = {"green": Band.GREEN, "vert": Band.GREEN, "orange": Band.ORANGE}
    chosen: dict[int, Finding] = {}
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        if part.lower() in bands:
            band = bands[part.lower()]
            chosen.update({f.number: f for f in findings if f.band is band})
            continue
        start_text, dash, end_text = part.partition("-")
        try:
            start = int(start_text)
            end = int(end_text) if dash else start
        except ValueError as exc:
            raise SelectionError(f"sélection invalide : {part!r}") from exc
        if start > end:
            raise SelectionError(f"intervalle invalide : {part!r}")
        for number in range(start, end + 1):
            if number not in by_number:
                raise SelectionError(f"aucune détection numéro {number}")
            chosen[number] = by_number[number]
    if not chosen:
        raise SelectionError("sélection vide")
    return [chosen[n] for n in sorted(chosen)]


def _why(finding: Finding, *, details: bool) -> str:
    """Evidence found; with details, also the evidence that was looked for in vain."""
    parts = [f"base {finding.rule.base} %"]
    for result in finding.evidence:
        if result.matched:
            parts.append(f"✔ {result.evidence.label} ({result.evidence.weight:+d})")
        elif details:
            parts.append(f"✘ {result.evidence.label} (absent, {result.evidence.weight:+d})")
    if finding.rule.max_confidence < 100:
        parts.append(f"plafonné à {finding.rule.max_confidence} %")
    return " ; ".join(parts)


def render_findings(
    findings: Sequence[Finding], *, root_display: str, min_confidence: int, details: bool
) -> str:
    lines: list[str] = []
    totals: dict[Band, tuple[int, int]] = {}
    for f in findings:
        count, size = totals.get(f.band, (0, 0))
        totals[f.band] = (count + 1, size + (f.allocated_size or 0))
    lines.append(f"{len(findings)} détection(s), confiance minimale {min_confidence} %")
    for band in (Band.GREEN, Band.ORANGE, Band.RED):
        if band in totals:
            count, size = totals[band]
            lines.append(
                f"  {BAND_LABELS[band]:<10} {count:>4} élément(s) {human_size(size):>10}  "
                f"({BAND_MEANING[band]})"
            )
    for f in findings:
        lines.append("")
        lines.append(
            f"{f.number:>3}. {BAND_LABELS[f.band]} {f.confidence:>3} %  "
            f"{human_size(f.allocated_size):>12}  {f.rule.label}"
        )
        lines.append(f"     {f.rel_path_display}")
        lines.append(f"     pourquoi : {_why(f, details=details)}")
        if details:
            lines.append(f"     note     : {f.rule.explanation}")
        command = recreate_command(f, root_display)
        if command:
            lines.append(f"     recréer  : {command}")
        elif f.rule.recreate_note:
            lines.append(f"     recréer  : {f.rule.recreate_note}")
        if not f.displayable:
            lines.append(
                "     ⚠ chemin avec caractères de contrôle ou UTF-8 invalide : "
                "aucune commande proposée"
            )
    return "\n".join(lines)


def render_selection(
    selected: Sequence[Finding], action: Action, *, root_display: str, scan_id: int
) -> str:
    date = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    result = cleanup_command(
        action,
        selected,
        root_display,
        archive_name=f"scan{scan_id}-{date}.tar.gz",
    )
    total = sum(f.allocated_size or 0 for f in selected)
    lines = [
        f"{len(selected)} élément(s) sélectionné(s), {human_size(total)} — "
        f"{ACTION_LABELS[action]}.",
        "TreeAudit n'exécute rien : relis la commande, puis copie-la dans un terminal.",
    ]
    risky = [f for f in selected if f.band is not Band.GREEN]
    if risky:
        numbers = ", ".join(str(f.number) for f in risky)
        lines.append(f"⚠ Sélection hors du vert ({numbers}) : vérifie ces éléments avant.")
    if action is Action.ARCHIVE:
        lines.append(
            "⚠ Une archive sur le même disque que l'original n'est pas une vraie sauvegarde."
        )
    for f in result.skipped:
        lines.append(f"⚠ Ignoré (nom non affichable sans risque) : n° {f.number}")
    lines.append("")
    lines.append(result.text if result.text else "(aucune commande possible pour cette sélection)")
    return "\n".join(lines)
