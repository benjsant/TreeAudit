"""Filter criteria: validation and compilation (docs/FILTERS_AND_REGEX.md).

Filters search the scan index. They never decide whether a path may be
accessed, and they are never turned into deletion rules. Every pattern is
compiled once per search; invalid patterns raise FilterError with a message
meant for the user (French UI).
"""

from __future__ import annotations

import fnmatch
import importlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from treeaudit.core.errors import TreeAuditError

MAX_PATTERN_LENGTH = 1000
_SIZE = re.compile(r"(?i)\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?o|i?b)?\s*")
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_EXTENSION = re.compile(r"[^/\x00]{1,32}")


class FilterError(TreeAuditError):
    """A filter criterion is invalid; the message is shown to the user."""


class EntryKindFilter(StrEnum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """What the user asked for. Every criterion is optional; all must match."""

    extensions: tuple[str, ...] = ()
    name_contains: str | None = None
    """Simple search: the name contains this text, whatever the case."""
    name_exact: str | None = None
    name_glob: str | None = None
    path_glob: str | None = None
    name_regex: str | None = None
    path_regex: str | None = None
    full_match: bool = False
    """Regexes must match the whole name/path (fullmatch) instead of a part (search)."""
    ignore_case: bool = False
    kinds: tuple[EntryKindFilter, ...] = ()
    min_size: int | None = None
    max_size: int | None = None


@dataclass(frozen=True, slots=True)
class Matcher:
    """A compiled pattern and how it applies: to the whole text, or to a part of it."""

    pattern: re.Pattern[str]
    whole: bool

    def __call__(self, text: str) -> bool:
        if self.whole:
            return self.pattern.fullmatch(text) is not None
        return self.pattern.search(text) is not None


@dataclass(frozen=True, slots=True)
class CompiledFilter:
    spec: FilterSpec
    extensions: tuple[str, ...]
    name_matchers: tuple[Matcher, ...] = ()
    path_matchers: tuple[Matcher, ...] = ()
    name_exact: str | None = None
    name_contains: str | None = None

    def matches(self, name: str, rel_path: str) -> bool:
        """Text criteria only; extension, kind and size are applied in SQL."""
        if self.name_contains is not None and self.name_contains not in name.casefold():
            return False
        if self.name_exact is not None:
            candidate = name.casefold() if self.spec.ignore_case else name
            if candidate != self.name_exact:
                return False
        return all(m(name) for m in self.name_matchers) and all(
            m(rel_path) for m in self.path_matchers
        )

    @property
    def needs_text(self) -> bool:
        return bool(
            self.name_matchers
            or self.path_matchers
            or self.name_exact is not None
            or self.name_contains is not None
        )


def parse_size(text: str) -> int:
    """'1024', '10k', '2.5M', '1 Gio', '3GB' -> bytes (binary units)."""
    match = _SIZE.fullmatch(text)
    if not match:
        raise FilterError(f"taille invalide : {text!r} (exemples : 500, 10k, 2.5M, 1G)")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])


def parse_extensions(text: str) -> tuple[str, ...]:
    """'jpg, .PNG,jpeg' -> ('jpg', 'png', 'jpeg'); extensions are stored lower-cased."""
    items = []
    for raw in text.split(","):
        item = raw.strip().lstrip(".").lower()
        if not item:
            continue
        if not _EXTENSION.fullmatch(item) or "." in item:
            raise FilterError(f"extension invalide : {raw.strip()!r}")
        items.append(item)
    if not items:
        raise FilterError("aucune extension donnée")
    return tuple(dict.fromkeys(items))


def _check_length(pattern: str, what: str) -> None:
    if not pattern:
        raise FilterError(f"{what} vide")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise FilterError(
            f"{what} trop long ({len(pattern)} caractères, maximum {MAX_PATTERN_LENGTH})"
        )
    if "\x00" in pattern:
        raise FilterError(f"{what} contient un caractère nul")


# --- regex safety (ReDoS) ---------------------------------------------------------------


def _load_parser() -> Any:
    """re._parser: private but stable since Python 3.11. Without it, only the
    pattern length limit and the search time budget apply."""
    try:
        return importlib.import_module("re._parser")
    except ImportError:  # pragma: no cover
        return None


_sre_parser: Any = _load_parser()


def _repeat_ops() -> frozenset[object]:
    if _sre_parser is None:  # pragma: no cover
        return frozenset()
    ops = {_sre_parser.MAX_REPEAT, _sre_parser.MIN_REPEAT}
    possessive = getattr(_sre_parser, "POSSESSIVE_REPEAT", None)
    if possessive is not None:
        ops.add(possessive)
    return frozenset(ops)


_REPEATS = _repeat_ops()


def _children(op: object, args: object) -> list[object]:
    """Sub-patterns of a parsed regex node (re._parser format)."""
    if _sre_parser is None or not isinstance(args, tuple | list):  # pragma: no cover
        return []
    if op in _REPEATS:
        return [args[2]]  # (min, max, subpattern)
    if op == _sre_parser.BRANCH:
        return list(args[1])  # (None, [alternatives])
    if op == _sre_parser.SUBPATTERN:
        return [args[-1]]  # (group, add_flags, del_flags, subpattern)
    if op in (_sre_parser.ASSERT, _sre_parser.ASSERT_NOT):
        return [args[1]]  # (direction, subpattern)
    if op == _sre_parser.GROUPREF_EXISTS:
        return [a for a in args[1:] if a is not None]  # (group, yes, no)
    return []


def _risky(nodes: object, inside_repeat: bool) -> str | None:
    """Detect what makes backtracking explode: a repeat inside a repeat, or a
    repeated alternation. Bounded repeats like {2,5} count too (conservative)."""
    for op, args in nodes:  # type: ignore[attr-defined]
        repeat = (
            op in _REPEATS
            and isinstance(args, tuple)
            and (args[1] == getattr(_sre_parser, "MAXREPEAT", None) or args[1] > 1)
        )
        if repeat and inside_repeat:
            return "quantificateurs imbriqués, par exemple (a+)+"
        if inside_repeat and _sre_parser is not None and op == _sre_parser.BRANCH:
            return "alternative répétée, par exemple (a|aa)*"
        for child in _children(op, args):
            found = _risky(child, inside_repeat or repeat)
            if found:
                return found
    return None


def compile_regex(pattern: str, *, ignore_case: bool, what: str) -> re.Pattern[str]:
    _check_length(pattern, what)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        where = f" (position {exc.pos})" if exc.pos is not None else ""
        raise FilterError(f"{what} invalide : {exc.msg}{where}") from exc
    if _sre_parser is not None:
        problem = _risky(_sre_parser.parse(pattern, flags), inside_repeat=False)
        if problem:
            raise FilterError(
                f"{what} refusée : {problem}. Ce genre de motif peut bloquer la recherche "
                "très longtemps ; réécris-le sans répétition imbriquée."
            )
    return compiled


# --- globs ----------------------------------------------------------------------------------


def _segment(segment: str) -> str:
    """One path segment of a glob: * and ? never cross '/'."""
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            while index + 1 < len(segment) and segment[index + 1] == "*":
                index += 1  # "**" inside a segment behaves like "*"
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = segment.find("]", index + 2)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = segment[index + 1 : end]
                negate = body.startswith(("!", "^"))
                body = body[1:] if negate else body
                escaped = body.replace("\\", "\\\\").replace("[", "\\[")
                out.append(f"[{'^' if negate else ''}{escaped}]")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def path_glob_to_regex(pattern: str) -> str:
    """Path glob relative to the root: '*' and '?' stay in one segment, '**' spans any."""
    if pattern.startswith("/"):
        raise FilterError("le glob de chemin est relatif à la racine : retire le « / » initial")
    segments = pattern.split("/")
    if any(segment == "" for segment in segments):
        raise FilterError("le glob de chemin contient un segment vide (« // » ou « / » final)")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            # Zero or more whole segments; unambiguous ([^/]* cannot eat '/').
            parts.append(".*" if last else "(?:[^/]*/)*")
            continue
        parts.append(_segment(segment) + ("" if last else "/"))
    return "".join(parts)


def compile_filter(spec: FilterSpec) -> CompiledFilter:
    """Validate and compile every criterion once."""
    flags = re.IGNORECASE if spec.ignore_case else 0
    names: list[Matcher] = []
    paths: list[Matcher] = []

    if spec.name_glob is not None:
        _check_length(spec.name_glob, "glob de nom")
        if "/" in spec.name_glob:
            raise FilterError("un glob de nom ne contient pas « / » : utilise le glob de chemin")
        names.append(Matcher(re.compile(fnmatch.translate(spec.name_glob), flags), whole=True))
    if spec.path_glob is not None:
        _check_length(spec.path_glob, "glob de chemin")
        paths.append(Matcher(re.compile(path_glob_to_regex(spec.path_glob), flags), whole=True))
    if spec.name_regex is not None:
        regex = compile_regex(spec.name_regex, ignore_case=spec.ignore_case, what="regex de nom")
        names.append(Matcher(regex, whole=spec.full_match))
    if spec.path_regex is not None:
        regex = compile_regex(spec.path_regex, ignore_case=spec.ignore_case, what="regex de chemin")
        paths.append(Matcher(regex, whole=spec.full_match))
    name_contains = None
    if spec.name_contains is not None:
        _check_length(spec.name_contains, "texte recherché")
        name_contains = spec.name_contains.casefold()
    name_exact = None
    if spec.name_exact is not None:
        _check_length(spec.name_exact, "nom exact")
        name_exact = spec.name_exact.casefold() if spec.ignore_case else spec.name_exact
    for bound in (spec.min_size, spec.max_size):
        if bound is not None and bound < 0:
            raise FilterError("une taille ne peut pas être négative")
    if spec.min_size is not None and spec.max_size is not None and spec.min_size > spec.max_size:
        raise FilterError("taille minimale supérieure à la taille maximale")

    return CompiledFilter(
        spec=spec,
        extensions=spec.extensions,
        name_matchers=tuple(names),
        path_matchers=tuple(paths),
        name_exact=name_exact,
        name_contains=name_contains,
    )


def _size_text(size: int) -> str:
    for unit, factor in (("Gio", 1024**3), ("Mio", 1024**2), ("Kio", 1024)):
        if size >= factor:
            return f"{size / factor:.4g} {unit}"
    return f"{size} o"


def describe(spec: FilterSpec) -> str:
    """Human description of a filter, kept in reports (French UI)."""
    parts: list[str] = []
    if spec.extensions:
        parts.append("extension " + ", ".join(f".{e}" for e in spec.extensions))
    if spec.name_contains is not None:
        parts.append(f"nom contenant {spec.name_contains!r}")
    if spec.name_exact is not None:
        parts.append(f"nom exact {spec.name_exact!r}")
    if spec.name_glob is not None:
        parts.append(f"nom correspondant à {spec.name_glob!r}")
    if spec.path_glob is not None:
        parts.append(f"chemin correspondant à {spec.path_glob!r}")
    mode = "entier" if spec.full_match else "partiel"
    if spec.name_regex is not None:
        parts.append(f"regex de nom {spec.name_regex!r} ({mode})")
    if spec.path_regex is not None:
        parts.append(f"regex de chemin {spec.path_regex!r} ({mode})")
    if spec.kinds:
        parts.append("type " + "/".join(k.value for k in spec.kinds))
    if spec.min_size is not None:
        parts.append(f"taille ≥ {_size_text(spec.min_size)}")
    if spec.max_size is not None:
        parts.append(f"taille ≤ {_size_text(spec.max_size)}")
    text = " ET ".join(parts) if parts else "aucun critère (tout le scan)"
    return text + (" ; casse ignorée" if spec.ignore_case else "")
