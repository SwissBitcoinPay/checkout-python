"""The tests' own data and the package's sources: nothing in them names the product's former
brand, a currency's name in words included (a franc of one country would): the fixtures hold
currency codes alone, as the rate sources' answers need nothing else."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Built from pieces: this file holds no mention of it either.
FORMER_BRAND = re.compile("sw" + "iss|s" + "bp", re.IGNORECASE)


def test_no_fixture_nor_source_names_the_former_brand() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{number}"
        for directory in ("tests", "src")
        for path in sorted((ROOT / directory).rglob("*"))
        if path.is_file() and path.suffix in (".py", ".json", ".csv", ".md", ".txt")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORMER_BRAND.search(line)
    ]
    assert found == []
