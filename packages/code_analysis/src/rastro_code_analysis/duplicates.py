from collections import defaultdict
from collections.abc import Sequence

from rastro_code_analysis.models import FileAnalysis, SymbolKey


def find_exact_duplicates(
    files: Sequence[FileAnalysis], *, min_lines: int = 3
) -> list[list[SymbolKey]]:
    """Group symbols whose normalized AST hash is identical (exact duplicates).

    Classes are skipped (their hash covers every method, which are already compared one by
    one) and so are very short symbols, which collide by accident (`get x() { return 1 }`).
    Each returned group has at least two members.
    """
    groups: dict[str, list[SymbolKey]] = defaultdict(list)
    for analysis in files:
        for symbol in analysis.symbols:
            if symbol.kind == "class" or symbol.end_line - symbol.start_line + 1 < min_lines:
                continue
            groups[symbol.body_hash].append((analysis.file_path, symbol.qualified_name))
    return [sorted(keys) for keys in groups.values() if len(keys) > 1]
