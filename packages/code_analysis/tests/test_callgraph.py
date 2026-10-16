from rastro_code_analysis import analyze_source, resolve_call_graph

CALENDAR = """
export function isFriday(day) { return day === 5; }
export function helper() { return 1; }
"""

AGENDA = """
import { isFriday } from './calendar';

export function validateBooking(day, hour) {
  return !(isFriday(day) && hour >= 12);
}

export class AgendaService {
  validate(day) { return validateBooking(day, 9); }
  book(day) { return this.validate(day); }
  static create() { return new AgendaService(); }
}

export function make() {
  const s = AgendaService.create();
  return s.book(1);
}

export function unresolved() {
  return externalLib.go(missing());
}
"""


def graph() -> set[tuple[tuple[str, str], tuple[str, str]]]:
    files = [analyze_source(CALENDAR, "calendar.ts"), analyze_source(AGENDA, "agenda.ts")]
    return resolve_call_graph(files)


def test_bare_call_resolves_across_files_when_unique() -> None:
    assert (("agenda.ts", "validateBooking"), ("calendar.ts", "isFriday")) in graph()


def test_bare_call_resolves_in_same_file() -> None:
    assert (("agenda.ts", "AgendaService.validate"), ("agenda.ts", "validateBooking")) in graph()


def test_this_call_resolves_to_method_of_same_class() -> None:
    assert (("agenda.ts", "AgendaService.book"), ("agenda.ts", "AgendaService.validate")) in graph()


def test_new_expression_resolves_to_class() -> None:
    assert (("agenda.ts", "AgendaService.create"), ("agenda.ts", "AgendaService")) in graph()


def test_static_call_resolves_by_qualified_name() -> None:
    assert (("agenda.ts", "make"), ("agenda.ts", "AgendaService.create")) in graph()


def test_member_call_on_variable_resolves_when_method_name_is_unique() -> None:
    assert (("agenda.ts", "make"), ("agenda.ts", "AgendaService.book")) in graph()


def test_unknown_calls_produce_no_edges() -> None:
    callers = {caller for caller, _ in graph()}

    assert ("agenda.ts", "unresolved") not in callers


def test_ambiguous_member_call_produces_no_edge() -> None:
    a = "class A { save() { return 1; } }"
    b = "class B { save() { return 2; } }"
    c = "export function run(repo) { return repo.save(); }"
    files = [analyze_source(s, f"{i}.ts") for i, s in enumerate([a, b, c])]

    assert resolve_call_graph(files) == set()


def test_ambiguous_bare_call_in_other_files_produces_no_edge() -> None:
    a = "export function util() { return 1; }"
    b = "export function util() { return 2; }"
    c = "export function run() { return util(); }"
    files = [analyze_source(s, f"{i}.ts") for i, s in enumerate([a, b, c])]

    assert resolve_call_graph(files) == set()


def test_recursion_has_no_self_edge() -> None:
    files = [analyze_source("function f(n) { return n ? f(n - 1) : 0; }", "a.ts")]

    assert resolve_call_graph(files) == set()
