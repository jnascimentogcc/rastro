"""Map a unified diff onto the symbols it touches."""

import re
from collections.abc import Sequence

from rastro_code_analysis.models import Symbol

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

LineRange = tuple[int, int]  # 1-based, inclusive, in the NEW version of the file


def changed_line_ranges(patch: str) -> list[LineRange]:
    """Line ranges of the new file that a patch adds or modifies.

    A pure deletion is reported as a one-line range at the line where the removed text used to
    sit, so a function that only lost a statement still counts as touched.
    """
    lines: list[int] = []
    new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        match = _HUNK.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            continue
        if not in_hunk or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            lines.append(new_line)
            new_line += 1
        elif raw.startswith("-"):
            lines.append(new_line)
        else:
            new_line += 1
    return _merge(sorted(set(lines)))


def _merge(lines: Sequence[int]) -> list[LineRange]:
    ranges: list[LineRange] = []
    for line in lines:
        if ranges and line <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return ranges


def _overlaps(symbol: Symbol, ranges: Sequence[LineRange]) -> bool:
    return any(start <= symbol.end_line and end >= symbol.start_line for start, end in ranges)


def touched_symbols(symbols: Sequence[Symbol], patch: str | None) -> list[Symbol]:
    """Symbols whose lines overlap the patch. Without a patch (binary or oversized diff) every
    symbol counts as touched.

    A class is reported only when a changed line falls inside it but outside all its methods
    (a field, a decorator, the signature); otherwise the touched methods already tell the story.
    """
    if patch is None:
        return list(symbols)
    ranges = changed_line_ranges(patch)
    touched: list[Symbol] = []
    for symbol in symbols:
        if not _overlaps(symbol, ranges):
            continue
        if symbol.kind == "class":
            methods = [
                s for s in symbols if s.qualified_name.startswith(symbol.qualified_name + ".")
            ]
            outside = [
                (start, end)
                for start, end in ranges
                if start <= symbol.end_line
                and end >= symbol.start_line
                and not any(m.start_line <= start and end <= m.end_line for m in methods)
            ]
            if not outside:
                continue
        touched.append(symbol)
    return touched


def symbol_source(content: str, symbol: Symbol) -> str:
    """The source lines of `symbol` (the analysis only stores line numbers, never code)."""
    lines = content.splitlines()
    return "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
