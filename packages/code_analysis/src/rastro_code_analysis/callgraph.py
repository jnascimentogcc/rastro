"""Approximate, name-based call resolution (no type checker, see spec section 7.4)."""

from collections import defaultdict
from collections.abc import Sequence

from rastro_code_analysis.models import CallRef, FileAnalysis, Symbol, SymbolKey

_Index = dict[str, list[tuple[SymbolKey, Symbol]]]


def resolve_call_graph(files: Sequence[FileAnalysis]) -> set[tuple[SymbolKey, SymbolKey]]:
    """Return (caller, callee) edges between symbols of the given files.

    Resolution rules:
    - `this.m()` -> `Class.m` in the caller's own file.
    - `Obj.m()` -> a symbol qualified `Obj.m` (static method), same file first.
    - bare `f()` / `new C()` -> a function or class named `f`/`C`: same file first, otherwise
      only when exactly one candidate exists in the repository.
    - `x.m()` on a variable or complex expression -> a method named `m`, only when exactly one
      such method exists (types are unknown, so ambiguity means no edge).
    Self-edges are dropped. Imports and aliases are not followed.
    """
    by_qualified: _Index = defaultdict(list)
    by_simple: _Index = defaultdict(list)
    for analysis in files:
        for symbol in analysis.symbols:
            entry = ((analysis.file_path, symbol.qualified_name), symbol)
            by_qualified[symbol.qualified_name].append(entry)
            by_simple[symbol.simple_name].append(entry)

    edges: set[tuple[SymbolKey, SymbolKey]] = set()
    for analysis in files:
        for symbol in analysis.symbols:
            caller = (analysis.file_path, symbol.qualified_name)
            for call in symbol.calls:
                callee = _resolve(call, symbol, analysis.file_path, by_qualified, by_simple)
                if callee is not None and callee != caller:
                    edges.add((caller, callee))
    return edges


def _resolve(
    call: CallRef, caller: Symbol, file_path: str, by_qualified: _Index, by_simple: _Index
) -> SymbolKey | None:
    if call.qualifier == "this":
        if "." not in caller.qualified_name:
            return None
        class_name = caller.qualified_name.rsplit(".", 1)[0]
        wanted = (file_path, f"{class_name}.{call.name}")
        same_class = by_qualified.get(wanted[1], [])
        return wanted if any(key == wanted for key, _ in same_class) else None

    if call.qualifier is not None:
        qualified = [key for key, _ in by_qualified.get(f"{call.qualifier}.{call.name}", [])]
        if qualified:
            return _prefer_same_file(qualified, file_path)

    candidates = by_simple.get(call.name, [])
    if call.is_member:
        methods = [key for key, sym in candidates if sym.kind == "method"]
        return methods[0] if len(methods) == 1 else None

    targets = [key for key, sym in candidates if sym.kind in ("function", "class")]
    return _prefer_same_file(targets, file_path)


def _prefer_same_file(candidates: list[SymbolKey], file_path: str) -> SymbolKey | None:
    same_file = [key for key in candidates if key[0] == file_path]
    if same_file:
        return same_file[0]
    return candidates[0] if len(candidates) == 1 else None
