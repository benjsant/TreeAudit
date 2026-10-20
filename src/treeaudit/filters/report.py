"""Text rendering of a search (French UI)."""

from __future__ import annotations

from datetime import UTC, datetime

from treeaudit.filters.search import SearchResult, Sort
from treeaudit.filters.spec import FilterSpec, describe
from treeaudit.parasites.report import human_size

_SORT_LABELS = {
    Sort.SIZE: "les plus gros",
    Sort.PATH: "par ordre alphabétique",
    Sort.MTIME: "les plus récents",
}
STATUS_LABELS = {
    "complete": "complet",
    "incomplete": "INCOMPLET",
    "cancelled": "ANNULÉ",
    "failed": "ÉCHEC",
    "running": "en cours",
    "pending": "en attente",
}
_KIND_LABELS = {"dir": "[dossier]", "symlink": "[lien]", "other": "[spécial]"}


def render_search(
    result: SearchResult,
    spec: FilterSpec,
    *,
    scan_id: int,
    scan_status: str,
    sort: Sort,
) -> str:
    lines = [
        f"Recherche dans le scan {scan_id} ({STATUS_LABELS.get(scan_status, scan_status)}) : "
        f"{describe(spec)}",
        f"  {result.total} résultat(s) : {result.files} fichier(s) "
        f"({human_size(result.files_size)}), {result.dirs} dossier(s), "
        f"{result.others} autre(s)",
    ]
    if result.partial_reason:
        lines.append(f"  ⚠ RÉSULTAT PARTIEL : {result.partial_reason}")
    if scan_status != "complete":
        lines.append("  ⚠ le scan n'est pas complet : des correspondances peuvent manquer")
    if result.not_displayable:
        lines.append(
            f"  ⚠ {result.not_displayable} chemin(s) avec UTF-8 invalide ou caractères de "
            "contrôle : comparés et affichés sous forme échappée (\\xNN)"
        )
    if result.examples:
        lines.append("")
        lines.append(f"Exemples ({len(result.examples)}, {_SORT_LABELS[sort]}) :")
        for match in result.examples:
            date = datetime.fromtimestamp(match.mtime_ns / 1e9, UTC).strftime("%Y-%m-%d")
            size = human_size(match.size) if match.size is not None else "?"
            label = _KIND_LABELS.get(match.kind, "")
            lines.append(f"  {size:>12}  {date}  {match.rel_path_display} {label}".rstrip())
    lines.append("")
    lines.append(
        "Ceci est une recherche : TreeAudit ne propose aucune commande de suppression "
        "à partir d'un filtre."
    )
    return "\n".join(lines)
