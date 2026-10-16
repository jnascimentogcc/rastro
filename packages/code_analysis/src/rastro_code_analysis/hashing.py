"""Normalized AST hash.

Two bodies get the same hash when they differ only by comments, formatting, the symbol's own
name, or the names of local variables and parameters. String and number literals are preserved,
so a different constant yields a different hash.
"""

import hashlib

from tree_sitter import Node

_SKIPPED_TOKENS = frozenset({";", ","})
_IDENTIFIER_TYPES = frozenset(
    {"identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"}
)
_PATTERN_HOLDERS = ("required_parameter", "optional_parameter", "variable_declarator")


def _identifiers_in(node: Node) -> list[str]:
    found: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _IDENTIFIER_TYPES:
            found.append(_text(current))
        else:
            stack.extend(current.children)
    return found


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _collect_locals(root: Node) -> set[str]:
    """Names declared inside `root`: parameters and variable declarations."""
    names: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _PATTERN_HOLDERS:
            target = node.child_by_field_name("pattern" if "parameter" in node.type else "name")
            if target is not None:
                names.update(_identifiers_in(target))
        elif node.type == "arrow_function":
            single = node.child_by_field_name("parameter")
            if single is not None:
                names.update(_identifiers_in(single))
        stack.extend(node.children)
    return names


def normalized_hash(
    symbol_node: Node, *, skip: Node | None = None, self_name: str | None = None
) -> str:
    """Hash `symbol_node`, ignoring the subtree `skip` (normally the symbol's name node).

    Recursive references to `self_name` are normalized so renaming a recursive function
    does not change its hash.
    """
    locals_ = _collect_locals(symbol_node)
    renames: dict[str, str] = {}
    parts: list[str] = []

    def visit(node: Node) -> None:
        if skip is not None and node.id == skip.id:
            return
        if node.type == "comment":
            return
        if node.type in _IDENTIFIER_TYPES:
            name = _text(node)
            if name in locals_:
                alias = renames.setdefault(name, f"v{len(renames)}")
                parts.append(f"{node.type}:{alias}")
            elif self_name is not None and name == self_name:
                parts.append(f"{node.type}:$self")
            else:
                parts.append(f"{node.type}:{name}")
            return
        if node.child_count == 0:
            if node.type in _SKIPPED_TOKENS:
                return
            token = '"' if node.type == "'" else node.type  # quote style is formatting
            parts.append(f"{node.type}:{_text(node)}" if node.is_named else token)
            return
        parts.append("(" + node.type)
        for child in node.children:
            visit(child)
        parts.append(")")

    visit(symbol_node)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
