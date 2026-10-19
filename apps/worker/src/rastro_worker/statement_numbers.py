"""The numbers a rule statement states, used to tell "same rule reworded" from "other limit".

Embeddings are dominated by phrasing and nearly ignore digits ("90 days" and "60 days" score 0.96),
so a rejected rule is only treated as repeated when the numbers match as well.

Out of scope: numbers written as words ("noventa", "ninety"), thousands separators ("1,000" is read
as 1.0) and units (``10%`` and ``10 days`` both yield 10).
"""

import re
from collections import Counter
from decimal import Decimal

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def statement_numbers(statement: str) -> Counter[str]:
    """The multiset of numbers in a statement, normalised (``2.50`` = ``2,5`` = ``2.5``; ``024`` = 24)."""
    found: Counter[str] = Counter()
    for token in _NUMBER.findall(statement):
        value = Decimal(token.replace(",", "."))
        found[format(value.normalize(), "f")] += 1
    return found


def same_numbers(first: str, second: str) -> bool:
    """True when both statements state exactly the same numbers (or neither states any)."""
    return statement_numbers(first) == statement_numbers(second)
