"""The native parser must not be bumped to a release that crashes the process."""

from importlib.metadata import version


def test_tree_sitter_stays_below_the_release_that_segfaults() -> None:
    """tree-sitter 0.26.0 died with an access violation while `analyze_source` walked the tree of
    an ordinary 10 KB TypeScript file (the worker exited silently mid `analyze_pr`; reproduced
    7 times out of 8, and never with 0.23 to 0.25). A crash in native code cannot be caught, so
    the cap in packages/code_analysis/pyproject.toml is the protection: if you lift it, first
    check that real files (large route files in particular) analyse without crashing."""
    major, minor = (int(part) for part in version("tree-sitter").split(".")[:2])
    assert (major, minor) < (0, 26)
