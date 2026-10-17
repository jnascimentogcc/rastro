from rastro_code_analysis import Declaration, analyze_source, touched_symbols

# The file from the first live test: the business threshold is a module constant.
POLICY = """const MIN_NOTICE_MS = 24 * 60 * 60 * 1000;

// With less than 24 hours of notice we cannot resell the slot.
export function canCancel(startsAt: Date, now: Date): boolean {
  return startsAt.getTime() - now.getTime() >= MIN_NOTICE_MS;
}
"""
POLICY_PATCH = (
    "@@ -1,4 +1,4 @@\n"
    "-const MIN_NOTICE_MS = 24 * 60 * 60 * 1000;\n"
    "+const MIN_NOTICE_MS = 12 * 60 * 60 * 1000;\n"
    " \n"
)


def names(symbols: list) -> list[str]:  # type: ignore[type-arg]
    return [s.qualified_name for s in symbols]


def test_module_level_constants_are_recorded_with_their_lines() -> None:
    analysis = analyze_source(POLICY, "src/policy.ts")

    assert analysis.declarations == (Declaration("MIN_NOTICE_MS", 1, 1),)


def test_several_declaration_forms() -> None:
    source = """export const LIMIT = 3, WINDOW = 7;
let counter = 0;
var legacy = 'x';
const { alpha, beta } = config;
enum Level {
  Low,
  High,
}
const handler = () => 1;
function run() { return LIMIT; }
"""

    found = {
        d.name: (d.start_line, d.end_line) for d in analyze_source(source, "a.ts").declarations
    }

    assert found == {
        "LIMIT": (1, 1),
        "WINDOW": (1, 1),
        "counter": (2, 2),
        "legacy": (3, 3),
        "alpha": (4, 4),
        "beta": (4, 4),
        "Level": (5, 8),
    }  # `handler` is a function symbol, not a constant


def test_nested_declarations_are_not_module_level() -> None:
    source = "function f() {\n  const inner = 1;\n  return inner;\n}\n"

    assert analyze_source(source, "a.ts").declarations == ()


def test_references_exclude_parameters_and_locals() -> None:
    source = """const RATE = 2;
const OFFSET = 1;
function price(base: number, RATE_LOCAL: number) {
  const OFFSET = 5;
  return base * RATE + OFFSET + RATE_LOCAL;
}
"""

    [price] = analyze_source(source, "a.ts").symbols

    assert "RATE" in price.references
    assert "OFFSET" not in price.references  # shadowed by a local
    assert "base" not in price.references and "RATE_LOCAL" not in price.references


def test_methods_and_arrow_functions_record_their_references() -> None:
    source = """const MAX = 3;
class Booking {
  allowed(n: number) { return n < MAX; }
}
const check = (n: number) => n < MAX;
"""

    symbols = {s.qualified_name: s for s in analyze_source(source, "a.ts").symbols}

    assert "MAX" in symbols["Booking.allowed"].references
    assert "MAX" in symbols["check"].references


def test_changing_a_constant_touches_the_functions_that_use_it() -> None:
    analysis = analyze_source(POLICY, "src/policy.ts")

    assert names(touched_symbols(analysis.symbols, POLICY_PATCH, analysis.declarations)) == [
        "canCancel"
    ]


def test_without_declarations_the_old_behaviour_is_unchanged() -> None:
    analysis = analyze_source(POLICY, "src/policy.ts")

    assert touched_symbols(analysis.symbols, POLICY_PATCH) == []


def test_only_users_of_the_changed_constant_are_touched() -> None:
    source = """const A = 1;
const B = 2;
function usesA() { return A; }
function usesB() { return B; }
function neither() { return 0; }
"""
    analysis = analyze_source(source, "a.ts")
    patch = "@@ -1,1 +1,1 @@\n-const A = 0;\n+const A = 1;\n"

    assert names(touched_symbols(analysis.symbols, patch, analysis.declarations)) == ["usesA"]


def test_a_class_is_not_reported_just_because_a_method_uses_the_constant() -> None:
    source = """const MAX = 3;
class Booking {
  allowed(n: number) { return n < MAX; }
  other() { return 1; }
}
"""
    analysis = analyze_source(source, "a.ts")
    patch = "@@ -1,1 +1,1 @@\n-const MAX = 2;\n+const MAX = 3;\n"

    assert names(touched_symbols(analysis.symbols, patch, analysis.declarations)) == [
        "Booking.allowed"
    ]


def test_a_symbol_changed_directly_and_through_a_constant_is_listed_once() -> None:
    analysis = analyze_source(POLICY, "src/policy.ts")
    patch = (
        "@@ -1,6 +1,6 @@\n"
        "-const MIN_NOTICE_MS = 24 * 60 * 60 * 1000;\n"
        "+const MIN_NOTICE_MS = 12 * 60 * 60 * 1000;\n"
        " \n"
        " // note\n"
        " export function canCancel(startsAt: Date, now: Date): boolean {\n"
        "-  return true;\n"
        "+  return startsAt.getTime() - now.getTime() >= MIN_NOTICE_MS;\n"
        " }\n"
    )

    assert names(touched_symbols(analysis.symbols, patch, analysis.declarations)) == ["canCancel"]
