"""Markers of the single pull request comment Rastro keeps up to date."""

# Hidden in the comment so it can be found again, and so a webhook for an edited comment can tell
# it is Rastro's own (the API must not import the worker, hence it lives here).
COMMENT_MARKER = "<!-- rastro:pr-impact -->"
