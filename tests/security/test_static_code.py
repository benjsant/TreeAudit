"""Static guard: production code must not run commands nor modify files.

This is a heuristic safety net over the AST (aliasing tricks could evade it); it
complements code review, it does not replace it. Writing is only allowed in the
modules listed in WRITE_ALLOWED, which only write inside the data directory.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "treeaudit"

# Relative to src/treeaudit; entries ending with "/" allow a whole package.
# These modules only write inside the data directory. reports/ will join later.
WRITE_ALLOWED = frozenset({"core/datadir.py", "db/"})


def _may_write(rel_path: str) -> bool:
    return any(
        rel_path.startswith(allowed) if allowed.endswith("/") else rel_path == allowed
        for allowed in WRITE_ALLOWED
    )


FORBIDDEN_MODULES = frozenset({"subprocess", "shutil", "pty", "ctypes", "tempfile"})
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})

# Functions or methods that execute programs: forbidden everywhere.
EXEC_ATTRS = frozenset(
    {
        "system",
        "popen",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "posix_spawn",
        "posix_spawnp",
        *(f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
        *(f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")),
    }
)

# os functions that modify the filesystem: only in WRITE_ALLOWED. Matched when
# called as os.<name>() or imported from os, so that str.replace() or list.remove()
# are not flagged.
OS_WRITE_FUNCS = frozenset(
    {
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "truncate",
        "ftruncate",
        "chmod",
        "fchmod",
        "lchmod",
        "chown",
        "fchown",
        "lchown",
        "link",
        "symlink",
        "mkfifo",
        "mknod",
        "utime",
        "setxattr",
        "removexattr",
        "mkdir",
        "makedirs",
        "write",
    }
)

# pathlib.Path methods that modify the filesystem, matched on any receiver: their
# names are specific enough. Path.replace() is not caught (same name as
# str.replace()): a known blind spot of this static heuristic.
PATH_WRITE_METHODS = frozenset(
    {
        "unlink",
        "rmdir",
        "rename",
        "write_text",
        "write_bytes",
        "touch",
        "chmod",
        "lchmod",
        "symlink_to",
        "hardlink_to",
        "mkdir",
    }
)

WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_TMPFILE"})


def _mode_argument(call: ast.Call, position: int) -> str | None:
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        value = call.args[position].value  # type: ignore[attr-defined]
        return value if isinstance(value, str) else None
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def find_violations(source: str, rel_path: str) -> list[str]:
    """Return human-readable violations found in ``source``."""
    tree = ast.parse(source)
    may_write = _may_write(rel_path)
    found: list[str] = []

    def report(node: ast.AST, message: str) -> None:
        found.append(f"{rel_path}:{getattr(node, 'lineno', '?')}: {message}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    report(node, f"import of {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in FORBIDDEN_MODULES:
                report(node, f"import from {node.module}")
            for alias in node.names:
                writes = module == "os" and alias.name in OS_WRITE_FUNCS
                if alias.name in EXEC_ATTRS or (writes and not may_write):
                    report(node, f"import of {alias.name} from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name is None:
                continue
            if isinstance(func, ast.Name) and name in FORBIDDEN_BUILTINS:
                report(node, f"call to {name}()")
            if name in EXEC_ATTRS:
                report(node, f"call to {name}()")
            on_os = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            if not may_write and (
                (on_os and name in OS_WRITE_FUNCS)
                or (isinstance(func, ast.Attribute) and name in PATH_WRITE_METHODS)
            ):
                report(node, f"call to {name}()")
            if name == "open" and not may_write:
                # builtin open(file, mode) / Path.open(mode); os.open(path, flags) has
                # no mode string and is checked through its flags below.
                position = 1 if isinstance(func, ast.Name) else 0
                mode = None if on_os else _mode_argument(node, position)
                if mode is not None and set(mode) & set("wax+"):
                    report(node, f"open() in write mode {mode!r}")
                flags = {
                    n.attr
                    for n in ast.walk(node)
                    if isinstance(n, ast.Attribute) and n.attr in WRITE_FLAGS
                }
                if flags:
                    report(node, f"os.open() with write flags {sorted(flags)}")
    return found


def _sources() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, f"no source files found under {SRC}"
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_command_execution_or_file_modification(path: Path) -> None:
    rel = path.relative_to(SRC).as_posix()
    assert find_violations(path.read_text(encoding="utf-8"), rel) == []


def test_write_allowlist_entries_exist() -> None:
    for rel in WRITE_ALLOWED:
        assert (SRC / rel).exists(), rel


@pytest.mark.parametrize(
    "snippet",
    [
        "import subprocess",
        "from shutil import rmtree",
        "import os\nos.system('ls')",
        "import os\nos.remove('x')",
        "import os\nos.replace('x', 'y')",
        "from os import unlink",
        "from pathlib import Path\nPath('x').unlink()",
        "from pathlib import Path\nPath('x').write_text('y')",
        "open('x', 'w')",
        "open('x', mode='a')",
        "from pathlib import Path\nPath('x').open('r+')",
        "import os\nos.open('x', os.O_WRONLY | os.O_CREAT)",
        "eval('1')",
        "import os\nos.execvp('ls', ['ls'])",
    ],
)
def test_checker_detects_forbidden_patterns(snippet: str) -> None:
    assert find_violations(snippet, "some/module.py"), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "open('x')",
        "open('x', 'rb')",
        "from pathlib import Path\nPath('x').read_bytes()",
        "import os\nos.open('x', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)",
        "import os\nos.lstat('x')",
        "'a-b'.replace('-', '_')",
        "[1].remove(1)",
    ],
)
def test_checker_allows_read_only_patterns(snippet: str) -> None:
    assert find_violations(snippet, "some/module.py") == []
