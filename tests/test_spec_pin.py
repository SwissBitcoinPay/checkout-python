"""The pinned copy of checkout-spec (PORTING.md, "The pinned copy of the specification")."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from spec_vectors import SPEC

TESTS = Path(__file__).resolve().parent


def _pin() -> dict[str, object]:
    with (SPEC / "PIN.json").open(encoding="utf-8") as file:
        pin: dict[str, object] = json.load(file)
    return pin


def test_pin_names_one_commit_of_checkout_spec() -> None:
    pin = _pin()
    assert pin["repository"] == "https://github.com/sattleme/checkout-spec"
    assert isinstance(pin["commit"], str)
    assert re.fullmatch("[0-9a-f]{40}", pin["commit"])
    assert pin["version"] == "1.0.0"
    assert pin["protocol"] == 1


def test_every_file_has_its_sha256_and_nothing_else_is_there() -> None:
    files = _pin()["files"]
    assert isinstance(files, dict)
    for relative, digest in files.items():
        data = (SPEC / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, relative
    present = {path.relative_to(SPEC).as_posix() for path in SPEC.rglob("*") if path.is_file()}
    assert present == {*files, "PIN.json"}


def test_every_vector_file_is_read_by_a_test() -> None:
    files = _pin()["files"]
    assert isinstance(files, dict)
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in TESTS.rglob("*.py")
        if path.name != Path(__file__).name
    )
    vectors = [name[len("vectors/") :] for name in files if name.startswith("vectors/")]
    missing = [name for name in vectors if f'"{name}"' not in sources]
    assert missing == []
    assert '"currencies.json"' in sources
