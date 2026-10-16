import pytest

from rastro_code_analysis import Symbol, UnsupportedLanguageError, analyze_source

SOURCE = """
import { x } from './x';

export function validateBooking(day: number, hour: number): boolean {
  return !(day === 5 && hour >= 12);
}

export const applyDiscount = (age: number, price: number) => {
  return age >= 65 ? price * 0.8 : price;
};

const legacy = function (a) { return a; };

export default class AgendaService {
  private readonly limit = 3;

  validate(day: number): boolean {
    return validateBooking(day, 9);
  }

  handler = () => this.validate(1);

  static create(): AgendaService {
    return new AgendaService();
  }
}

function outer() {
  function inner() { return 1; }
  return inner();
}
"""


def by_name(source: str, path: str = "a.ts") -> dict[str, Symbol]:
    return {s.qualified_name: s for s in analyze_source(source, path).symbols}


def test_extracts_functions_arrows_classes_and_methods() -> None:
    symbols = by_name(SOURCE)

    assert {name: s.kind for name, s in symbols.items()} == {
        "validateBooking": "function",
        "applyDiscount": "function",
        "legacy": "function",
        "AgendaService": "class",
        "AgendaService.validate": "method",
        "AgendaService.handler": "method",
        "AgendaService.create": "method",
        "outer": "function",
    }


def test_nested_functions_are_not_separate_symbols() -> None:
    assert "inner" not in by_name(SOURCE)


def test_line_ranges_are_one_based_and_inclusive() -> None:
    symbol = by_name(SOURCE)["validateBooking"]
    lines = SOURCE.splitlines()

    assert lines[symbol.start_line - 1].startswith("export function validateBooking")
    assert lines[symbol.end_line - 1] == "}"


def test_default_export_without_name_uses_file_stem() -> None:
    source = "export default function (a: number) { return a + 1; }"

    assert set(by_name(source, "src/utils/price.ts")) == {"price"}


def test_default_export_arrow_uses_file_stem() -> None:
    assert set(by_name("export default (a) => a * 2;", "double.js")) == {"double"}


def test_javascript_and_jsx_files_are_supported() -> None:
    source = "export function Button() { return <button>ok</button>; }"

    assert set(by_name(source, "Button.jsx")) == {"Button"}


def test_windows_paths_are_accepted() -> None:
    assert set(by_name("export default function () { return 1; }", "src\\utils\\tax.ts")) == {"tax"}


def test_unsupported_extension_raises() -> None:
    with pytest.raises(UnsupportedLanguageError):
        analyze_source("print(1)", "a.py")


def test_syntax_errors_do_not_raise() -> None:
    symbols = by_name("function ok() { return 1; }\nfunction broken( {{{\n")

    assert "ok" in symbols


def test_overloads_keep_a_single_symbol() -> None:
    source = """
    function f(a: string): string;
    function f(a: number): number;
    function f(a: any): any { return a; }
    """

    assert list(by_name(source)) == ["f"]


def test_accepts_bytes_and_str() -> None:
    assert analyze_source(b"function a() {}", "a.ts") == analyze_source("function a() {}", "a.ts")
