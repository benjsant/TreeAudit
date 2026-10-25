"""Loading and validation of the parasite rules catalogue (rules.toml)."""

from __future__ import annotations

import hashlib
import re
import shlex
import string
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from typing import Any

from treeaudit.core.errors import TreeAuditError
from treeaudit.shellcmd import PRIVILEGED

RULES_RESOURCE = "rules.toml"

# Simple globs only: *, ? and [abc]. No negated classes, so that SQLite GLOB and
# fnmatch agree, and no path separator.
_GLOB_PATTERN = re.compile(r"[^/\x00]+")
_NEGATED_CLASS = re.compile(r"\[[!^]")
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_ALLOWED_PLACEHOLDERS = frozenset({"name"})


class RuleError(TreeAuditError):
    """The rules catalogue is invalid."""


class Target(StrEnum):
    DIR = "dir"
    FILE = "file"


class EvidenceKind(StrEnum):
    CHILD = "child"
    CHILD_GLOB = "child_glob"
    SIBLING = "sibling"
    SIBLING_GLOB = "sibling_glob"
    ALL_FILES_GLOB = "all_files_glob"
    ANY_FILE_GLOB = "any_file_glob"
    ANY_FILE_NOT_GLOB = "any_file_not_glob"

    @property
    def is_glob(self) -> bool:
        return self is not EvidenceKind.CHILD and self is not EvidenceKind.SIBLING


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    value: str
    weight: int
    label: str


@dataclass(frozen=True, slots=True)
class Recreate:
    command: str
    when_sibling: str | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    label: str
    target: Target
    names: tuple[str, ...]
    globs: tuple[str, ...]
    base: int
    explanation: str
    evidence: tuple[Evidence, ...] = ()
    recreate: tuple[Recreate, ...] = ()
    recreate_note: str | None = None
    max_confidence: int = 100


_RULE_KEYS = frozenset(
    {
        "id",
        "label",
        "target",
        "names",
        "globs",
        "base",
        "explanation",
        "evidence",
        "recreate",
        "recreate_note",
        "max_confidence",
    }
)
_EVIDENCE_KEYS = frozenset({"kind", "value", "weight", "label"})
_RECREATE_KEYS = frozenset({"command", "when_sibling"})


def _fail(where: str, message: str) -> RuleError:
    return RuleError(f"{where}: {message}")


def _required(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _fail(where, f"{key} must be a non-empty string")
    return value


def _optional(table: dict[str, Any], key: str, where: str) -> str | None:
    return None if table.get(key) is None else _required(table, key, where)


def _int(table: dict[str, Any], key: str, where: str, low: int, high: int, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise _fail(where, f"{key} must be an integer between {low} and {high}")
    return value


def _check_keys(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise _fail(where, f"unknown key(s): {', '.join(unknown)}")


def _check_name(value: str, where: str) -> str:
    if "/" in value or "\x00" in value or value in (".", ".."):
        raise _fail(where, f"invalid name {value!r}")
    return value


def _check_glob(value: str, where: str) -> str:
    if not _GLOB_PATTERN.fullmatch(value) or _NEGATED_CLASS.search(value):
        raise _fail(where, f"invalid glob {value!r} (use *, ? and [abc] only)")
    return value


_DESTRUCTIVE = frozenset({"rm", "rmdir", "shred", "dd", "mkfs", "truncate", "unlink", "mv", "find"})


def _check_template(command: str, where: str) -> str:
    for _, field, _, _ in string.Formatter().parse(command):
        if field is not None and field not in _ALLOWED_PLACEHOLDERS:
            raise _fail(where, f"unknown placeholder {{{field}}} in recreate command")
    # A recreate command builds things back: it never needs privileges nor deletes.
    try:
        words = {word.rpartition("/")[2] for word in shlex.split(command)}
    except ValueError as exc:
        raise _fail(where, f"unreadable recreate command: {exc}") from exc
    forbidden = words & (PRIVILEGED | _DESTRUCTIVE)
    if forbidden:
        raise _fail(where, f"forbidden program(s) in recreate command: {sorted(forbidden)}")
    return command


def _string_list(table: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(where, f"{key} must be a list of strings")
    return tuple(value)


def _parse_rule(raw: Any, index: int) -> Rule:
    where = f"rule[{index}]"
    if not isinstance(raw, dict):
        raise _fail(where, "must be a table")
    _check_keys(raw, _RULE_KEYS, where)
    rule_id = _required(raw, "id", where)
    if not _RULE_ID.fullmatch(rule_id):
        raise _fail(where, f"invalid id {rule_id!r}")
    where = f"rule {rule_id}"

    target_value = raw.get("target")
    if target_value not in (Target.DIR.value, Target.FILE.value):
        raise _fail(where, "target must be 'dir' or 'file'")
    target = Target(str(target_value))

    names = tuple(_check_name(n, where) for n in _string_list(raw, "names", where))
    globs = tuple(_check_glob(g, where) for g in _string_list(raw, "globs", where))
    if not names and not globs:
        raise _fail(where, "needs at least one name or glob")

    evidence: list[Evidence] = []
    raw_evidence = raw.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise _fail(where, "evidence must be a list")
    for item in raw_evidence:
        if not isinstance(item, dict):
            raise _fail(where, "each evidence must be a table")
        _check_keys(item, _EVIDENCE_KEYS, where)
        kind_value = item.get("kind")
        if kind_value not in {k.value for k in EvidenceKind}:
            raise _fail(where, f"unknown evidence kind {kind_value!r}")
        kind = EvidenceKind(str(kind_value))
        value = _required(item, "value", where)
        label = _required(item, "label", where)
        value = _check_glob(value, where) if kind.is_glob else _check_name(value, where)
        weight = _int(item, "weight", where, -100, 100, 0)
        evidence.append(Evidence(kind, value, weight, label))

    recreate: list[Recreate] = []
    raw_recreate = raw.get("recreate", [])
    if not isinstance(raw_recreate, list):
        raise _fail(where, "recreate must be a list")
    for item in raw_recreate:
        if not isinstance(item, dict):
            raise _fail(where, "each recreate entry must be a table")
        _check_keys(item, _RECREATE_KEYS, where)
        command = _required(item, "command", where)
        when = _optional(item, "when_sibling", where)
        recreate.append(
            Recreate(
                command=_check_template(command, where),
                when_sibling=None if when is None else _check_name(when, where),
            )
        )

    return Rule(
        id=rule_id,
        label=_required(raw, "label", where),
        target=target,
        names=names,
        globs=globs,
        base=_int(raw, "base", where, 0, 100, 0),
        explanation=_required(raw, "explanation", where),
        evidence=tuple(evidence),
        recreate=tuple(recreate),
        recreate_note=_optional(raw, "recreate_note", where),
        max_confidence=_int(raw, "max_confidence", where, 0, 100, 100),
    )


def parse_rules(raw: dict[str, Any]) -> tuple[Rule, ...]:
    _check_keys(raw, frozenset({"rule"}), "rules")
    items = raw.get("rule", [])
    if not isinstance(items, list) or not items:
        raise RuleError("rules: at least one [[rule]] is required")
    rules = tuple(_parse_rule(item, index) for index, item in enumerate(items))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RuleError(f"duplicate rule id: {rule.id}")
        seen.add(rule.id)
    return rules


def _catalogue_text() -> str:
    return resources.files("treeaudit.parasites").joinpath(RULES_RESOURCE).read_text("utf-8")


def rules_fingerprint() -> str:
    """Short digest of the shipped catalogue: changes whenever a rule changes."""
    return hashlib.sha256(_catalogue_text().encode()).hexdigest()[:16]


def load_default_rules() -> tuple[Rule, ...]:
    """Load the catalogue shipped with TreeAudit."""
    text = _catalogue_text()
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"invalid TOML in {RULES_RESOURCE}: {exc}") from exc
    return parse_rules(raw)
