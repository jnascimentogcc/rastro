from pathlib import PurePosixPath

import tree_sitter_typescript as ts_grammars
from tree_sitter import Language, Parser, Tree

_TYPESCRIPT = Language(ts_grammars.language_typescript())
_TSX = Language(ts_grammars.language_tsx())

# `.ts` cannot use the TSX grammar (`<T>x` casts clash with JSX); JavaScript may contain JSX,
# and TSX is a superset of JavaScript syntax.
_LANGUAGE_BY_SUFFIX = {
    ".ts": _TYPESCRIPT,
    ".mts": _TYPESCRIPT,
    ".cts": _TYPESCRIPT,
    ".tsx": _TSX,
    ".js": _TSX,
    ".jsx": _TSX,
    ".mjs": _TSX,
    ".cjs": _TSX,
}

SUPPORTED_SUFFIXES = frozenset(_LANGUAGE_BY_SUFFIX)


class UnsupportedLanguageError(ValueError):
    pass


def is_supported(file_path: str) -> bool:
    return PurePosixPath(file_path.replace("\\", "/")).suffix.lower() in SUPPORTED_SUFFIXES


def parse(source: bytes, file_path: str) -> Tree:
    suffix = PurePosixPath(file_path.replace("\\", "/")).suffix.lower()
    language = _LANGUAGE_BY_SUFFIX.get(suffix)
    if language is None:
        raise UnsupportedLanguageError(f"unsupported file type: {file_path}")
    # tree-sitter is error tolerant: syntax errors still yield a usable tree.
    return Parser(language).parse(source)
