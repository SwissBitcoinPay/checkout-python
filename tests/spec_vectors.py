"""The pinned copy of the specification (spec/): the only place the tests read vectors from."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SPEC = Path(__file__).resolve().parent.parent / "spec"


def vector(relative: str) -> Any:
    """A vector file of the pinned copy, parsed."""
    with (SPEC / "vectors" / relative).open(encoding="utf-8") as file:
        return json.load(file)


def spec_file(relative: str) -> Any:
    with (SPEC / relative).open(encoding="utf-8") as file:
        return json.load(file)
