"""The tidying script and its undo script (D-041), never run by TreeAudit.

Paths are relative to the chosen folder, where the script first goes, and
quoted with shlex.quote. Before each move the script checks the element is
still there and, for a file, has the size and date the analysis saw; the
target must be free and no folder on its way a link (it would lead outside the
chosen folder); mv -n never overwrites. The undo script moves everything back
in reverse order, then removes the folders the tidying may have created, if
they are empty: never another empty folder (security review V1).
"""

from __future__ import annotations

import posixpath
import shlex

from treeaudit.organize.plan import Plan

_HEADER = """\
#!/bin/bash
# TreeAudit — {title}
# Analyse n°{scan_id} du {date}. RELIS ce script avant de le lancer.
# Il ne supprime rien et n'écrase rien (mv -n) ; un élément modifié depuis
# l'analyse, absent, ou dont la place est déjà prise est ignoré et signalé.
set -u
cd -- {folder} || exit 1
done_count=0
skipped=0
skip() {{ printf 'ignoré : %s (%s)\\n' "$1" "$2" >&2; skipped=$((skipped + 1)); }}
linked() {{  # a folder on the way to $1 is a link: it would lead elsewhere
    local path=$1
    while [[ $path == */* ]]; do
        path=${{path%/*}}
        if [[ -L $path ]]; then return 0; fi
    done
    return 1
}}
place() {{
    if [[ -e $2 || -L $2 ]]; then skip "$1" "la place est déjà prise : $2"; return; fi
    if linked "$2"; then skip "$1" "un dossier du chemin est un lien : $2"; return; fi
    mkdir -p -- "$(dirname -- "$2")" || {{ skip "$1" "dossier impossible à créer"; return; }}
    if mv -n -- "$1" "$2" && [[ ( -e $2 || -L $2 ) && ! -e $1 && ! -L $1 ]]; then
        done_count=$((done_count + 1))
    else
        skip "$1" "déplacement impossible"
    fi
}}
"""

_MOVE_FUNCTIONS = """\
file() {  # source cible taille date
    if [[ ! -f $1 || -L $1 ]]; then skip "$1" "absent ou n'est plus un fichier"; return; fi
    local seen
    seen=$(stat -c '%s %Y' -- "$1")
    if [[ $seen != "$3 $4" ]]; then skip "$1" "modifié depuis l'analyse"; return; fi
    place "$1" "$2"
}
folder() {  # source cible
    if [[ ! -d $1 || -L $1 ]]; then skip "$1" "absent ou n'est plus un dossier"; return; fi
    place "$1" "$2"
}
"""

_UNDO_FUNCTIONS = """\
back() {  # place actuelle, place d'origine
    if [[ ! -e $1 && ! -L $1 ]]; then skip "$1" "introuvable"; return; fi
    place "$1" "$2"
}
"""


def _q(path: str) -> str:
    return shlex.quote(path)


def _check_moves(plan: Plan) -> None:
    """Every path of the script stays inside the chosen folder (double check).

    The plan already builds them so; this refuses to write a script otherwise.
    """
    for path in (plan.destination, *(p for m in plan.moves for p in (m.source, m.target))):
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or posixpath.normpath(path) != path
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError(f"unsafe path in tidying plan: {path!r}")


def _created_folders(plan: Plan) -> list[str]:
    """Every folder above a target, the deepest first: what mkdir -p may have made."""
    folders = {plan.destination}
    for move in plan.moves:
        parent = posixpath.dirname(move.target)
        while parent and parent not in folders:
            folders.add(parent)
            parent = posixpath.dirname(parent)
    return sorted(folders, key=lambda f: (-f.count("/"), f))


def tidy_script(plan: Plan, folder: str, *, title: str, scan_id: int, date: str) -> str:
    _check_moves(plan)
    lines = [
        _HEADER.format(title=title, scan_id=scan_id, date=date, folder=_q(folder)),
        _MOVE_FUNCTIONS,
    ]
    for move in plan.moves:
        if move.kind == "file":
            lines.append(f"file {_q(move.source)} {_q(move.target)} {move.size} {move.mtime_s}")
        else:
            lines.append(f"folder {_q(move.source)} {_q(move.target)}")
    lines.append(
        'echo "$done_count élément(s) rangé(s), $skipped ignoré(s). '
        "Pour tout remettre en place : le script d'annulation.\""
    )
    return "\n".join(lines) + "\n"


def undo_script(plan: Plan, folder: str, *, title: str, scan_id: int, date: str) -> str:
    _check_moves(plan)
    lines = [
        _HEADER.format(
            title=f"annulation : {title}", scan_id=scan_id, date=date, folder=_q(folder)
        ),
        _UNDO_FUNCTIONS,
    ]
    for move in reversed(plan.moves):
        lines.append(f"back {_q(move.target)} {_q(move.source)}")
    lines.append(
        "# Only the folders the tidying may have created, children first, if empty"
        " (rmdir refuses the others)."
    )
    lines += [
        f"[[ -d {_q(folder)} && ! -L {_q(folder)} ]] && rmdir -- {_q(folder)} 2>/dev/null"
        for folder in _created_folders(plan)
    ]
    lines.append('echo "$done_count élément(s) remis en place, $skipped ignoré(s)."')
    return "\n".join(lines) + "\n"
