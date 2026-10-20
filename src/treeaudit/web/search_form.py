"""Search form of the web interface (query string -> FilterSpec)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from treeaudit.filters.spec import (
    EntryKindFilter,
    FilterError,
    FilterSpec,
    parse_extensions,
    parse_size,
)

_TEXT = Field(default="", max_length=1000)


class SearchForm(BaseModel):
    """Every field is optional; empty fields are ignored."""

    go: str = Field(default="", max_length=10)
    ext: str = _TEXT
    name: str = _TEXT
    glob: str = _TEXT
    path_glob: str = _TEXT
    regex: str = _TEXT
    path_regex: str = _TEXT
    full_match: bool = False
    ignore_case: bool = False
    kind: Literal["", "file", "dir", "symlink", "other"] = ""
    min_size: str = Field(default="", max_length=32)
    max_size: str = Field(default="", max_length=32)
    sort: Literal["size", "path", "mtime"] = "size"
    limit: int = Field(default=50, ge=1, le=1000)

    @property
    def submitted(self) -> bool:
        return bool(self.go)


def spec_from_form(form: SearchForm) -> FilterSpec:
    """Build the filter; raises FilterError with a message for the user."""

    def text(value: str) -> str | None:
        return value if value.strip() else None

    try:
        kinds = (EntryKindFilter(form.kind),) if form.kind else ()
    except ValueError as exc:  # pragma: no cover - restricted by the Literal above
        raise FilterError("type inconnu") from exc
    return FilterSpec(
        extensions=parse_extensions(form.ext) if form.ext.strip() else (),
        name_exact=text(form.name),
        name_glob=text(form.glob),
        path_glob=text(form.path_glob),
        name_regex=text(form.regex),
        path_regex=text(form.path_regex),
        full_match=form.full_match,
        ignore_case=form.ignore_case,
        kinds=kinds,
        min_size=parse_size(form.min_size) if form.min_size.strip() else None,
        max_size=parse_size(form.max_size) if form.max_size.strip() else None,
    )
