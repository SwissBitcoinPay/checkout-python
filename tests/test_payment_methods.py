"""The methods of an order by its amount (SPEC.md §31, Methods by amount)."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout._methods import methods_after_leasing, methods_by_amount
from sattleme_checkout._order import check_sats
from sattleme_checkout.errors import InvalidOrderError, SattlemeError
from spec_vectors import vector

METHODS: Any = vector("payment-methods.json")


def _methods(case: dict[str, Any]) -> list[str]:
    sats = check_sats(case["sats"])
    sendable = case["sendable"]
    methods = methods_by_amount(sats, int(sendable["min"]), int(sendable["max"]), param="sats")
    if methods.onchain:
        methods = methods_after_leasing(methods, case["address"])
    return [name for name in ("lightning", "onchain") if getattr(methods, name)]


@pytest.mark.parametrize("case", METHODS["cases"], ids=lambda c: c["name"])
def test_payment_methods(case: dict[str, Any]) -> None:
    expected = case["expected"]
    if "error" not in expected:
        assert _methods(case) == expected["methods"]
        return
    with pytest.raises(SattlemeError) as raised:
        _methods(case)
    assert raised.value.code == expected["error"]
    if "param" in expected:
        assert isinstance(raised.value, InvalidOrderError)
        assert raised.value.param == expected["param"]


def test_an_order_in_a_currency_names_fiat() -> None:
    with pytest.raises(InvalidOrderError) as raised:
        methods_by_amount(999, 1000, 4_000_000, param="fiat")
    assert raised.value.param == "fiat"
