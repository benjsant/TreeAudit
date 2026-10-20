"""Text rendering of duplicate groups and of the proposed command (French UI)."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime

from treeaudit.duplicates.groups import Copy, DuplicateGroup, GroupReport
from treeaudit.parasites.report import human_size
from treeaudit.shellcmd import Action, cleanup_command

ACTION_LABELS = {
    Action.TRASH: "mise à la corbeille (réversible)",
    Action.DELETE: "suppression DÉFINITIVE",
}


class SelectionError(ValueError):
    """The --select / --keep values are invalid."""


def _date(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, UTC).strftime("%Y-%m-%d")


EMPTY_WARNING = (
    "⚠ Fichiers vides : les supprimer ne libère aucune place, et certains programmes en "
    "ont besoin (marqueurs, verrous, jeux). Ils ne sont jamais inclus dans --select all : "
    "désigne explicitement les zones voulues."
)


def render_groups(report: GroupReport, *, limit: int, copies: int = 10) -> str:
    groups = report.groups
    removable = sum(len(g.removable) for g in groups)
    recoverable = sum(g.recoverable for g in groups)
    title = (
        "Micro-nettoyage (fichiers de moins de 1 Kio et fichiers vides)"
        if report.micro
        else ("Doublons exacts")
    )
    lines = [
        title,
        f"  {len(groups)} groupe(s) ; {removable} copie(s) supprimable(s) ; "
        f"{human_size(recoverable)} récupérables",
    ]
    if report.protected_copies:
        lines.append(
            f"  🔒 {report.protected_copies} copie(s) protégée(s) jamais proposées "
            "(fichiers-marqueurs, dépôts git, logiciels et dépendances installés)"
        )
    if report.hardlinked_inodes:
        lines.append(
            f"  (i) {report.hardlinked_inodes} fichier(s) à liens physiques : "
            "plusieurs noms, une seule place sur le disque ; ce ne sont pas des doublons"
        )
    if any(g.is_empty_files for g in groups):
        lines.append(f"  {EMPTY_WARNING}")
    for group in groups[:limit]:
        lines.append("")
        kind = (
            f"fichiers vides dans {group.zone}"
            if group.is_empty_files
            else "contenu identique (SHA-256)"
        )
        lines.append(
            f"G{group.number}  {len(group.copies)} copies de {human_size(group.size)} → "
            f"{human_size(group.recoverable)} récupérables, "
            f"{len(group.removable)} élément(s) en moins   ({kind})"
        )
        kept = {c.entry_id for c in group.keep}
        shown = [
            (index, copy)
            for index, copy in enumerate(group.copies, start=1)
            if index <= copies or copy.entry_id in kept
        ]
        for index, copy in shown:
            if copy.protection:
                mark = "🔒"
            elif copy.entry_id in kept:
                mark = "★ "
            else:
                mark = "  "
            lines.append(f"   {mark}{index:>3}. {copy.rel_path_display}   ({_date(copy.mtime_ns)})")
        if len(shown) < len(group.copies):
            lines.append(f"        … {len(group.copies) - len(shown)} autre(s) copie(s) (--copies)")
        lines.append(f"      à garder : {' ; '.join(group.keep_reasons)}")
    if len(groups) > limit:
        lines.append("")
        lines.append(f"… {len(groups) - limit} groupe(s) de plus (--limit pour en voir davantage)")
    return "\n".join(lines)


def parse_groups(text: str, groups: Sequence[DuplicateGroup]) -> list[DuplicateGroup]:
    """Accept group numbers and ranges ("1,3,5-7", with or without "G") or "all"."""
    by_number = {g.number: g for g in groups}
    if text.strip().lower() in ("all", "tout", "tous"):
        # Empty files are never selected in bulk (see EMPTY_WARNING).
        selected = [g for g in groups if not g.is_empty_files]
        if not selected:
            raise SelectionError("aucun groupe (les fichiers vides se choisissent un par un)")
        return selected
    chosen: dict[int, DuplicateGroup] = {}
    for part in (p.strip().lstrip("Gg") for p in text.split(",")):
        if not part:
            continue
        start_text, dash, end_text = part.partition("-")
        try:
            start = int(start_text)
            end = int(end_text.strip().lstrip("Gg")) if dash else start
        except ValueError as exc:
            raise SelectionError(f"sélection invalide : {part!r}") from exc
        if start > end:
            raise SelectionError(f"intervalle invalide : {part!r}")
        for number in range(start, end + 1):
            if number not in by_number:
                raise SelectionError(f"aucun groupe G{number}")
            chosen[number] = by_number[number]
    if not chosen:
        raise SelectionError("sélection vide")
    return [chosen[n] for n in sorted(chosen)]


def override_keep(group: DuplicateGroup, keep_index: int) -> DuplicateGroup:
    """Keep the copy numbered ``keep_index`` (1-based) instead of the suggestion.

    Protected copies stay kept whatever the choice.
    """
    if not 1 <= keep_index <= len(group.copies):
        raise SelectionError(f"G{group.number} n'a pas de copie numéro {keep_index}")
    chosen = group.copies[keep_index - 1]
    keep = tuple(c for c in group.copies if c.protection or c is chosen)
    return dataclasses.replace(group, keep=keep, keep_reasons=("choisie par toi",))


def render_command(groups: Sequence[DuplicateGroup], action: Action, *, root_display: str) -> str:
    targets: list[Copy] = [c for g in groups for c in g.removable]
    result = cleanup_command(action, targets, root_display)
    freed = sum(g.recoverable for g in groups)
    lines = [
        f"{len(targets)} copie(s) de {len(groups)} groupe(s), {human_size(freed)} — "
        f"{ACTION_LABELS[action]}.",
        "Pour chaque groupe, la copie marquée ★ (et les copies 🔒) sont conservées.",
        "TreeAudit n'exécute rien : relis la commande, puis copie-la dans un terminal.",
    ]
    for copy in result.skipped:
        lines.append(f"⚠ Ignorée (nom non affichable sans risque) : {copy.rel_path_display!r}")
    lines.append("")
    lines.append(result.text if result.text else "(aucune commande possible pour cette sélection)")
    return "\n".join(lines)
