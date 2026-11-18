"""Checkout links (SPEC.md §30): origins, keys, URLs, fragments, records, redirect URLs."""

from __future__ import annotations

from typing import Any

import pytest

from sattleme_checkout._channel import checkout_origin_of
from sattleme_checkout._checkout import (
    Fragment,
    Shown,
    link_url,
    parse_fragment,
    read_link_value,
)
from sattleme_checkout._crypto import Secret
from sattleme_checkout._derive import link_keys
from sattleme_checkout._records import NewerFormat
from sattleme_checkout._urls import redirect_url_shown
from spec_vectors import vector

LINKS: Any = vector("checkout-links.json")
REDIRECTS: Any = vector("redirect-urls.json")


@pytest.mark.parametrize("case", LINKS["origins"], ids=lambda c: repr(c["checkout"]))
def test_origin(case: dict[str, Any]) -> None:
    assert checkout_origin_of(case["checkout"]) == case["expected"]


@pytest.mark.parametrize("case", LINKS["derivations"], ids=lambda c: c["linkKey"][:8])
def test_derivations(case: dict[str, str]) -> None:
    keys = link_keys(Secret(bytes.fromhex(case["linkKey"])))
    assert keys.address == case["address"]
    assert keys.encryption_key.reveal().hex() == case["encryptionKey"]


@pytest.mark.parametrize("case", LINKS["urls"], ids=lambda c: c["origin"])
def test_url(case: dict[str, Any]) -> None:
    url = link_url(
        case["origin"], case["author"], Secret(bytes.fromhex(case["linkKey"])), case["relays"]
    )
    assert url == case["url"]


@pytest.mark.parametrize("case", LINKS["fragments"], ids=lambda c: c["name"])
def test_fragment(case: dict[str, Any]) -> None:
    parsed = parse_fragment(case["fragment"])
    expected = case["expected"]
    if isinstance(expected, str):
        assert parsed == expected
    else:
        assert isinstance(parsed, Fragment)
        assert {
            "author": parsed.author,
            "linkKey": parsed.link_key,
            "relays": list(parsed.relays),
        } == expected


def _shown(value: object) -> Any:
    try:
        shown = read_link_value(value)
    except NewerFormat:
        return "newer"
    if shown is None:
        return "invalid"
    assert isinstance(shown, Shown)
    return {
        "sats": str(shown.sats),
        "paymentHash": shown.payment_hash,
        "onchainAddress": shown.onchain_address,
        "expiresAt": shown.expires_at,
        "redirect": shown.redirect,
        "rest": None
        if shown.rest is None
        else {"author": shown.rest.author, "key": shown.rest.key},
    }


@pytest.mark.parametrize("case", LINKS["records"], ids=lambda c: c["name"])
def test_record(case: dict[str, Any]) -> None:
    assert _shown(case["value"]) == case["expected"]


@pytest.mark.parametrize("case", REDIRECTS["cases"], ids=lambda c: repr(c["url"])[:60])
def test_redirect_url(case: dict[str, Any]) -> None:
    expected = case["expected"]
    shown = redirect_url_shown(case["url"])
    if isinstance(expected, dict) and "error" in expected:
        assert shown is None
    else:
        assert shown == expected["shown"]


@pytest.mark.parametrize(
    "left_out", [("invoice",), ("verifyUrl",), ("invoice", "verifyUrl")], ids="+".join
)
def test_a_version_2_record_without_its_invoice_members_is_invalid(left_out: tuple[str]) -> None:
    """SPEC.md §30, Reading: version 2's `invoice` and `verifyUrl` are both null (an order paid
    on-chain only), or an invoice; absent is not null (SPEC.md §2): an invalid link."""
    record = next(r for r in LINKS["records"] if r["name"] == "version 2: an address only")
    value = dict(record["value"])
    assert read_link_value(value) is not None
    for name in left_out:
        del value[name]
    assert read_link_value(value) is None
