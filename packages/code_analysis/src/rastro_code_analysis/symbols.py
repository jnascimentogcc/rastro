from collections.abc import Callable
from pathlib import PurePosixPath

from tree_sitter import Node

from rastro_code_analysis.hashing import normalized_hash
from rastro_code_analysis.models import CallRef, FileAnalysis, Symbol, SymbolKind
from rastro_code_analysis.parser import parse

_FUNCTION_DECLARATIONS = frozenset({"function_declaration", "generator_function_declaration"})
_FUNCTION_VALUES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)
_CLASS_NODES = frozenset({"class_declaration", "abstract_class_declaration", "class"})

SymbolSink = Callable[[Symbol], None]


def _text(node: Node | None) -> str:
    return (node.text or b"").decode("utf-8", errors="replace") if node is not None else ""


def analyze_source(source: str | bytes, file_path: str) -> FileAnalysis:
    """Extract symbols from a TypeScript/JavaScript file.

    Symbols: named functions, arrow/function expressions assigned to variables, classes and
    their methods. Functions nested inside other functions are part of the enclosing symbol.
    Raises UnsupportedLanguageError for other file types.
    """
    data = source.encode("utf-8") if isinstance(source, str) else source
    root = parse(data, file_path).root_node
    stem = PurePosixPath(file_path.replace("\\", "/")).stem
    collected: dict[str, Symbol] = {}

    def add(symbol: Symbol) -> None:
        collected.setdefault(symbol.qualified_name, symbol)  # overloads/accessors: keep first

    for node in root.children:
        _visit_top_level(node, stem, add)
    return FileAnalysis(file_path=file_path, symbols=tuple(collected.values()))


def _visit_top_level(node: Node, file_stem: str, add: SymbolSink) -> None:
    if node.type == "export_statement":
        inner = node.child_by_field_name("declaration") or node.child_by_field_name("value")
        if inner is not None:
            _visit_declaration(inner, file_stem, add, exported_default=True)
        return
    _visit_declaration(node, file_stem, add)


def _visit_declaration(
    node: Node, file_stem: str, add: SymbolSink, *, exported_default: bool = False
) -> None:
    if node.type in _FUNCTION_DECLARATIONS:
        name_node = node.child_by_field_name("name")
        add(_function_symbol(node, _text(name_node), "function", name_node))
    elif node.type in ("lexical_declaration", "variable_declaration"):
        for declarator in node.named_children:
            _visit_declarator(declarator, add)
    elif node.type in _CLASS_NODES:
        name_node = node.child_by_field_name("name")
        _visit_class(node, _text(name_node) or file_stem, name_node, add)
    elif exported_default and node.type in _FUNCTION_VALUES:
        add(_function_symbol(node, file_stem, "function", node.child_by_field_name("name")))


def _visit_declarator(declarator: Node, add: SymbolSink) -> None:
    if declarator.type != "variable_declarator":
        return
    name_node = declarator.child_by_field_name("name")
    value = declarator.child_by_field_name("value")
    if name_node is None or value is None or name_node.type != "identifier":
        return
    if value.type in _FUNCTION_VALUES:
        add(_function_symbol(value, _text(name_node), "function", None, outer=declarator))


def _visit_class(node: Node, class_name: str, name_node: Node | None, add: SymbolSink) -> None:
    add(
        Symbol(
            qualified_name=class_name,
            kind="class",
            body_hash=normalized_hash(node, skip=name_node, self_name=class_name),
            start_line=node.start_point.row + 1,
            end_line=node.end_point.row + 1,
            calls=_collect_calls(node),
        )
    )
    body = node.child_by_field_name("body")
    if body is None:
        return
    for member in body.named_children:
        if member.type == "method_definition":
            member_name = member.child_by_field_name("name")
            add(
                _function_symbol(
                    member,
                    f"{class_name}.{_text(member_name)}",
                    "method",
                    member_name,
                    self_name=_text(member_name),
                )
            )
        elif member.type in ("public_field_definition", "field_definition"):
            value = member.child_by_field_name("value")
            field_name = member.child_by_field_name("name") or member.child_by_field_name(
                "property"
            )
            if value is not None and field_name is not None and value.type in _FUNCTION_VALUES:
                add(
                    _function_symbol(
                        value,
                        f"{class_name}.{_text(field_name)}",
                        "method",
                        None,
                        outer=member,
                        self_name=_text(field_name),
                    )
                )


def _function_symbol(
    node: Node,
    qualified_name: str,
    kind: SymbolKind,
    name_node: Node | None,
    *,
    outer: Node | None = None,
    self_name: str | None = None,
) -> Symbol:
    span = outer or node
    simple = self_name if self_name is not None else qualified_name.rsplit(".", 1)[-1]
    return Symbol(
        qualified_name=qualified_name,
        kind=kind,
        body_hash=normalized_hash(node, skip=name_node, self_name=simple),
        start_line=span.start_point.row + 1,
        end_line=span.end_point.row + 1,
        calls=_collect_calls(node),
    )


def _collect_calls(node: Node) -> tuple[CallRef, ...]:
    calls: dict[CallRef, None] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("call_expression", "new_expression"):
            ref = _call_ref(current)
            if ref is not None:
                calls[ref] = None
        stack.extend(current.children)
    return tuple(sorted(calls, key=lambda c: (c.qualifier or "", c.name, c.is_member)))


def _call_ref(call: Node) -> CallRef | None:
    target = call.child_by_field_name("function") or call.child_by_field_name("constructor")
    if target is None:
        return None
    if target.type == "identifier":
        return CallRef(name=_text(target))
    if target.type == "member_expression":
        prop = target.child_by_field_name("property")
        obj = target.child_by_field_name("object")
        if prop is None or obj is None:
            return None
        if obj.type == "this":
            return CallRef(name=_text(prop), qualifier="this", is_member=True)
        if obj.type == "identifier":
            return CallRef(name=_text(prop), qualifier=_text(obj), is_member=True)
        return CallRef(name=_text(prop), is_member=True)
    return None
