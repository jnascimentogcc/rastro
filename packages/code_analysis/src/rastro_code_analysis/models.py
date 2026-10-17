from dataclasses import dataclass, field
from typing import Literal

SymbolKind = Literal["function", "method", "class"]
SymbolKey = tuple[str, str]  # (file_path, qualified_name)


@dataclass(frozen=True)
class CallRef:
    """A call site found inside a symbol body.

    `is_member` is True for `x.name()` style calls. `qualifier` is `"this"` or the identifier the
    method is called on (`foo` in `foo.bar()`); it is None for bare calls (`bar()`) and for
    calls on complex expressions (`a.b.c()`).
    """

    name: str
    qualifier: str | None = None
    is_member: bool = False


@dataclass(frozen=True)
class Declaration:
    """A module-level `const`/`let`/`var` (or enum) that is not a function. It is not a symbol of
    its own, but business thresholds often live here (`const MIN_NOTICE = 24 * HOUR`), so a change
    to it is a change to every symbol that references it."""

    name: str
    start_line: int  # 1-based, inclusive
    end_line: int


@dataclass(frozen=True)
class Symbol:
    qualified_name: str
    kind: SymbolKind
    body_hash: str
    start_line: int  # 1-based, inclusive
    end_line: int
    calls: tuple[CallRef, ...] = field(default_factory=tuple)
    # Free identifiers used in the body (not parameters or locals): module constants, imports...
    references: tuple[str, ...] = field(default_factory=tuple)

    @property
    def simple_name(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class FileAnalysis:
    file_path: str
    symbols: tuple[Symbol, ...]
    declarations: tuple[Declaration, ...] = field(default_factory=tuple)
