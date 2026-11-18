"""BlockCypher's `received` (SPEC.md §31, Explorers): as BlockCypher writes it, nothing else."""

from __future__ import annotations

import pytest

from sattleme_checkout._explorer import parse_received, received_time
from sattleme_checkout.errors import NetworkError

BASE = 1_791_637_512_000  # 2026-10-10T13:05:12Z


@pytest.mark.parametrize(
    ("text", "milliseconds"),
    [
        ("2026-10-10T13:05:12Z", BASE),
        ("2026-10-10T13:05:12.345Z", BASE + 345),
        ("2026-10-10T13:05:12.3Z", BASE + 300),
        ("2026-10-10T13:05:12.34Z", BASE + 340),
        # Past milliseconds: dropped, never rounded.
        ("2026-10-10T13:05:12.3456789Z", BASE + 345),
        ("2026-10-10T13:05:12.999999999Z", BASE + 999),
        ("2024-02-29T00:00:00Z", 1_709_164_800_000),
        ("2000-02-29T00:00:00Z", 951_782_400_000),
        ("1970-01-01T00:00:00.001Z", 1),
    ],
)
def test_a_time(text: str, milliseconds: int) -> None:
    assert parse_received(text) == milliseconds


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-10T13:05:12",
        "2026-10-10t13:05:12.345z",
        "2026-10-10T13:05:12.345z",
        "2026-10-10T14:05:12+01:00",
        "2026-10-10T13:05:12+00:00",
        "2026-10-10T13:05:12.1234567890Z",
        "2026-10-10 13:05:12Z",
        "2026-10-10T13:05Z",
        "2026-10-10T13:05:12.Z",
        "2026-10-10T13:05:12,345Z",
        " 2026-10-10T13:05:12Z",
        "2026-10-10T13:05:12Z ",
        "2026-10-10T13:05:12Z\n",
        "2026-13-10T13:05:12Z",
        "2026-00-10T13:05:12Z",
        "2026-04-31T13:05:12Z",
        "2026-02-29T13:05:12Z",
        "1900-02-29T13:05:12Z",
        "2026-10-10T24:00:00Z",
        "2026-10-10T13:60:12Z",
        "2026-10-10T23:59:60Z",
        "0000-01-01T00:00:00Z",
        "+2026-10-10T13:05:12Z",
        # Digits of another script (fullwidth): not ASCII digits.
        "".join(chr(0xFF10 + int(digit)) for digit in "2026") + "-10-10T13:05:12Z",
        "",
        None,
        1_791_637_512,
    ],
)
def test_not_blockcyphers(text: object) -> None:
    assert parse_received(text) is None


def test_a_received_time_is_above_0() -> None:
    """SPEC.md §31: read in Unix milliseconds, above 0; anything else is `network`."""
    assert received_time("1970-01-01T00:00:00.001Z") == 1
    for text in ("1970-01-01T00:00:00Z", "1970-01-01T00:00:00.0009Z", "1969-12-31T23:59:59Z"):
        with pytest.raises(NetworkError):
            received_time(text)
