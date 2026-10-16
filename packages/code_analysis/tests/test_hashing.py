from rastro_code_analysis import analyze_source, find_exact_duplicates


def hash_of(source: str, name: str, path: str = "a.ts") -> str:
    symbols = {s.qualified_name: s for s in analyze_source(source, path).symbols}
    return symbols[name].body_hash


BASE = """
function price(age: number, base: number) {
  if (age >= 65) {
    return base * 0.8;
  }
  return base;
}
"""


def test_renamed_function_has_same_hash() -> None:
    renamed = BASE.replace("function price", "function computePrice")

    assert hash_of(BASE, "price") == hash_of(renamed, "computePrice")


def test_renamed_locals_and_parameters_have_same_hash() -> None:
    renamed = BASE.replace("age", "years").replace("base", "amount")

    assert hash_of(BASE, "price") == hash_of(renamed, "price")


def test_formatting_comments_and_quotes_do_not_matter() -> None:
    reformatted = """
    function price(age: number, base: number) {
      // seniors pay less
      if (age >= 65) { return base * 0.8 }
      return base
    }
    """
    quoted_a = "function f(a) { return a + 'x'; }"
    quoted_b = 'function f(a) { return a + "x"; }'

    assert hash_of(BASE, "price") == hash_of(reformatted, "price")
    assert hash_of(quoted_a, "f") == hash_of(quoted_b, "f")


def test_changed_literal_changes_hash() -> None:
    assert hash_of(BASE, "price") != hash_of(BASE.replace("65", "60"), "price")
    assert hash_of(BASE, "price") != hash_of(BASE.replace("0.8", "0.9"), "price")


def test_changed_operator_or_structure_changes_hash() -> None:
    assert hash_of(BASE, "price") != hash_of(BASE.replace(">=", ">"), "price")
    assert hash_of(BASE, "price") != hash_of(BASE.replace("return base;", ""), "price")


def test_referenced_globals_are_not_normalized() -> None:
    a = "function f() { return taxA(1); }"
    b = "function f() { return taxB(1); }"

    assert hash_of(a, "f") != hash_of(b, "f")


def test_recursive_function_rename_keeps_hash() -> None:
    a = "function fact(n) { return n <= 1 ? 1 : n * fact(n - 1); }"
    b = "function factorial(n) { return n <= 1 ? 1 : n * factorial(n - 1); }"

    assert hash_of(a, "fact") == hash_of(b, "factorial")


def test_arrow_and_declaration_forms_share_hash_only_when_bodies_match() -> None:
    arrow = "const f = (a) => { return a + 1; };"
    arrow_renamed = "const g = (x) => { return x + 1; };"

    assert hash_of(arrow, "f") == hash_of(arrow_renamed, "g")


def test_method_rename_keeps_hash() -> None:
    a = "class A { run(x) { return x * 2; } }"
    b = "class B { execute(y) { return y * 2; } }"

    assert hash_of(a, "A.run") == hash_of(b, "B.execute")


def test_duplicates_across_files() -> None:
    body = "export function canBook(day, hour) {\n  return !(day === 5 && hour >= 12);\n}\n"
    copy = body.replace("canBook", "allowed").replace("day", "d").replace("hour", "h")
    other = "export function other(a) {\n  return a;\n\n}\n"
    files = [
        analyze_source(body, "src/agenda.ts"),
        analyze_source(copy, "src/calendar.ts"),
        analyze_source(other, "src/other.ts"),
    ]

    assert find_exact_duplicates(files) == [
        [("src/agenda.ts", "canBook"), ("src/calendar.ts", "allowed")]
    ]


def test_tiny_symbols_are_not_reported_as_duplicates() -> None:
    files = [
        analyze_source("function a() { return 1; }", "a.ts"),
        analyze_source("function b() { return 1; }", "b.ts"),
    ]

    assert find_exact_duplicates(files) == []
