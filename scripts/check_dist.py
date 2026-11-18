"""Check what `python -m build` made, before anyone publishes it: one wheel and one sdist of the
version pyproject.toml declares, the wheel holding the package alone (typed), the sdist the
pinned specification and the tests too, and the dependencies of pyproject.toml.

Usage: python scripts/check_dist.py dist/
"""

from __future__ import annotations

import email.parser
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "sattleme_checkout"


def fail(message: str) -> None:
    print(f"check_dist: {message}", file=sys.stderr)
    raise SystemExit(1)


def main(directory: Path) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    expected_wheel = f"{PACKAGE}-{version}-py3-none-any.whl"
    expected_sdist = f"{PACKAGE}-{version}.tar.gz"
    if [w.name for w in wheels] != [expected_wheel]:
        fail(f"expected {expected_wheel} alone, found {[w.name for w in wheels]}")
    if [s.name for s in sdists] != [expected_sdist]:
        fail(f"expected {expected_sdist} alone, found {[s.name for s in sdists]}")

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        info = f"{PACKAGE}-{version}.dist-info"
        outside = [n for n in names if not n.startswith((f"{PACKAGE}/", f"{info}/"))]
        if outside:
            fail(f"the wheel holds files outside the package: {outside}")
        for needed in (f"{PACKAGE}/__init__.py", f"{PACKAGE}/py.typed", f"{info}/licenses/LICENSE"):
            if needed not in names:
                fail(f"the wheel lacks {needed}")
        metadata = email.parser.Parser().parsestr(wheel.read(f"{info}/METADATA").decode())
    if metadata["Name"] != project["name"] or metadata["Version"] != version:
        fail(f"the wheel's metadata names {metadata['Name']} {metadata['Version']}")
    if metadata["License-Expression"] != project["license"]:
        fail(f"the wheel's license is {metadata['License-Expression']}")
    if metadata["Requires-Python"] != project["requires-python"]:
        fail(f"the wheel requires Python {metadata['Requires-Python']}")
    requires = sorted(metadata.get_all("Requires-Dist") or [])
    if requires != sorted(project["dependencies"]):
        fail(f"the wheel's dependencies are {requires}")

    with tarfile.open(sdists[0]) as sdist:
        names = sdist.getnames()
    top = f"{PACKAGE}-{version}"
    for needed in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "SECURITY.md",
        "spec/PIN.json",
        "spec/SPEC.md",
        f"src/{PACKAGE}/py.typed",
        "tests/test_spec_pin.py",
    ):
        if f"{top}/{needed}" not in names:
            fail(f"the sdist lacks {needed}")
    stray = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    if stray:
        fail(f"the sdist holds compiled files: {stray}")
    print(f"check_dist: {expected_wheel} and {expected_sdist} are as they should be")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: python scripts/check_dist.py dist/")
    main(Path(sys.argv[1]))
