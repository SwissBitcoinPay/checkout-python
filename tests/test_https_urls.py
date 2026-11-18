"""https URLs (SPEC.md §15) and the pay URLs of Lightning addresses (SPEC.md §16)."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout._lnurl import pay_url
from sattleme_checkout._urls import https_host, same_host
from spec_vectors import vector

URLS: Any = vector("https-urls.json")


@pytest.mark.parametrize("case", URLS["urls"], ids=lambda c: repr(c["url"])[:80])
def test_an_https_url(case: dict[str, Any]) -> None:
    found = https_host(case["url"])
    expected = case["expected"]
    assert found == (None if expected is None else (expected["host"], expected["port"]))


@pytest.mark.parametrize("case", URLS["sameHost"], ids=lambda c: f"{c['a']} {c['b']}")
def test_the_same_host(case: dict[str, Any]) -> None:
    assert same_host(case["a"], case["b"]) is case["expected"]
    assert same_host(case["b"], case["a"]) is case["expected"]


@pytest.mark.parametrize("case", URLS["lightningAddresses"], ids=lambda c: repr(c["address"])[:80])
def test_a_lightning_addresss_pay_url(case: dict[str, Any]) -> None:
    assert pay_url(case["address"]) == case["payUrl"]
