from rastro_code_analysis import (
    analyze_source,
    changed_line_ranges,
    symbol_source,
    touched_symbols,
)

SOURCE = """export function a() {
  return 1;
}

export class Service {
  limit = 3;

  book(day: number) {
    return day !== 5;
  }

  cancel(id: string) {
    return id;
  }
}
"""

PATCH_BOOK = """@@ -8,3 +8,3 @@ export class Service {
   book(day: number) {
-    return day !== 6;
+    return day !== 5;
   }
"""


def test_added_and_modified_lines_use_new_file_numbers() -> None:
    assert changed_line_ranges(PATCH_BOOK) == [(9, 9)]


def test_consecutive_changes_are_merged_and_deletions_become_points() -> None:
    patch = "@@ -1,5 +1,4 @@\n a\n-b\n-c\n+d\n+e\n f\n@@ -20,3 +19,2 @@\n x\n-gone\n y\n"
    assert changed_line_ranges(patch) == [(2, 3), (20, 20)]


def test_no_newline_marker_and_text_outside_hunks_are_ignored() -> None:
    patch = "diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"
    assert changed_line_ranges(patch) == [(1, 1)]


def symbols() -> list:  # type: ignore[type-arg]
    return list(analyze_source(SOURCE, "a.ts").symbols)


def test_only_the_method_that_changed_is_reported() -> None:
    touched = touched_symbols(symbols(), PATCH_BOOK)

    assert [s.qualified_name for s in touched] == ["Service.book"]


def test_class_is_reported_when_a_change_is_outside_its_methods() -> None:
    patch = "@@ -6,1 +6,1 @@\n-  limit = 2;\n+  limit = 3;\n"

    assert [s.qualified_name for s in touched_symbols(symbols(), patch)] == ["Service"]


def test_function_deletion_inside_body_counts() -> None:
    patch = "@@ -2,2 +2,1 @@\n-  const x = 1;\n   return 1;\n"

    assert [s.qualified_name for s in touched_symbols(symbols(), patch)] == ["a"]


def test_missing_patch_touches_everything() -> None:
    assert len(touched_symbols(symbols(), None)) == len(symbols())


def test_symbol_source_slices_the_symbol_lines() -> None:
    book = next(s for s in symbols() if s.qualified_name == "Service.book")

    assert symbol_source(SOURCE, book).splitlines()[0].strip() == "book(day: number) {"
    assert symbol_source(SOURCE, book).splitlines()[-1].strip() == "}"
