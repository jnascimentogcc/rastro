import os
import stat
from pathlib import Path

BEGIN = "# >>> rastro >>>"
END = "# <<< rastro <<<"
BLOCK = f"""{BEGIN}
rastro capture >/dev/null 2>&1 || true
{END}
"""
_SHELL_SHEBANGS = ("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env sh", "#!/usr/bin/env bash")


class HookError(RuntimeError):
    pass


def install_post_commit(hooks_dir: Path) -> str:
    """Add the Rastro block to `post-commit` without touching any existing hook logic.

    The block goes right after the shebang so an `exit` further down in an existing hook
    cannot skip it. Returns "created", "chained" or "unchanged". Raises HookError when the
    existing hook is not a shell script (it is left untouched).
    """
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / "post-commit"
    if not path.exists():
        path.write_text("#!/bin/sh\n" + BLOCK, encoding="utf-8", newline="\n")
        _make_executable(path)
        return "created"

    content = path.read_text(encoding="utf-8")
    if BEGIN in content:
        return "unchanged"
    lines = content.splitlines(keepends=True)
    first = lines[0].strip() if lines else ""
    if first.startswith("#!") and first not in _SHELL_SHEBANGS:
        raise HookError(
            f"{path} is not a shell script ({first}); add `rastro capture` to it manually"
        )
    updated = lines[0] + BLOCK + "".join(lines[1:]) if first.startswith("#!") else BLOCK + content
    path.write_text(updated, encoding="utf-8", newline="\n")
    _make_executable(path)
    return "chained"


def _make_executable(path: Path) -> None:
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
