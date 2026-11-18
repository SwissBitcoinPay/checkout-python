"""What the README must tell an integrator (SPEC.md §27, Security): the sentences a reader of the
package relies on, kept as the specification asks for them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"


def _text() -> str:
    """The README, its lines joined: a sentence is found whatever its line breaks."""
    return re.sub(r"\s+", " ", README.read_text(encoding="utf-8"))


def test_stored_invoices_are_the_shops() -> None:
    """SPEC.md §22, §27: a stored invoice is checked to be well formed, never to be this
    integration's; the shop checks only the invoices it stored itself. Never that each member
    must be there: `onchainAddress` may be left out (SPEC.md §22)."""
    text = _text()
    assert (
        "**Stored invoices are your shop's.** Check only the invoices your shop stored itself with "
        "their orders, where only it writes (its database), never one that comes from the "
        "customer (a form field, a URL, a cookie): `get_status` checks that a stored invoice is "
        "well formed (SPEC.md §22), never that this integration made it. An invoice someone else "
        "wrote, with a Lightning invoice of their own that they paid, reads as `paid` "
        "(SPEC.md §27)."
    ) in text
    assert "each of its members there" not in text


def test_it_says_whether_secp256k1_signs_and_computes_ecdh_in_constant_time() -> None:
    """SPEC.md §27: an implementation's README says whether its secp256k1 signs (BIP-340) and
    computes ECDH (NIP-44) in constant time, naming the library and what it does."""
    text = _text()
    section = text[text.index("## Security") : text.index("## Known limits")]
    for named in (
        "libsecp256k1",
        "`btclib-secp256k1`",
        "`secp256k1_schnorrsig_sign32`",
        "`secp256k1_ecdh`",
        "`secp256k1_ecmult_const`",
        "signing (BIP-340,",
        "the ECDH of NIP-44",
        "constant time",
    ):
        assert named in section, named
    # What the README names is what the package signs and computes ECDH with.
    from sattleme_checkout import _crypto

    assert vars(_crypto)["_ssa"].__name__ == "btclib_secp256k1.ssa"
    assert vars(_crypto)["_ecdh"].__name__ == "btclib_secp256k1.ecdh"


def _models() -> dict[str, object]:
    """One of every type the client gives (SPEC.md §3), each state of a status."""
    from sattleme_checkout.models import (
        DeclaredInvoice,
        Fiat,
        Invoice,
        Merchant,
        PaymentStatus,
        RateInfo,
        Rest,
    )

    fiat = Fiat(amount="24.50", currency="CHF", minor=2450, digits=2)
    rate = RateInfo(source="kraken", currency="CHF", price="69012.5", fetched_at=10, time=None)
    rest = Rest(sats=29_000, bolt11=None, payment_uri="bitcoin:bc1p?amount=0.00029")
    common: dict[str, object] = {
        "ref": "ORD-1",
        "bolt11": "lnbc1",
        "payment_hash": "ab" * 32,
        "verify_url": "https://breez.tips/v",
        "onchain_address": None,
        "payment_uri": "lightning:lnbc1",
        "sats": 49_000,
        "fiat": fiat,
        "note": "ORD-1",
        "created_at": 10,
        "expires_at": 910,
    }
    return {
        "Invoice": Invoice(**common, rate=rate, checkout_url=None, redirect_url=None),  # type: ignore[arg-type]
        "DeclaredInvoice": DeclaredInvoice(**common, declared_at=11),  # type: ignore[arg-type]
        "PaymentStatus open": PaymentStatus.open(910, 20_000, rest),
        "PaymentStatus unconfirmed": PaymentStatus.unconfirmed(49_000, 1, ("cd" * 32,)),
        "PaymentStatus paid in Lightning": PaymentStatus.paid_lightning("ef" * 32),
        "PaymentStatus paid on-chain": PaymentStatus.paid_onchain(49_000, ("cd" * 32,)),
        "PaymentStatus paid mixed": PaymentStatus.paid_mixed(49_000, ("cd" * 32,), ("ef" * 32,)),
        "PaymentStatus expired": PaymentStatus.expired(0),
        "Rest": rest,
        "Fiat": fiat,
        "RateInfo": rate,
        "Merchant": Merchant(business_name="Sunrise", min_sats=1, max_sats=10, onchain_addresses=3),
    }


def test_it_names_the_one_function_that_writes_the_specifications_json() -> None:
    """SPEC.md §3: the README names `to_json()` as what writes the specification's JSON, and says
    to use no other serializer: the language's own refuse these types, or write other JSON."""
    import dataclasses
    import json

    text = _text()
    assert "**The specification's JSON is `to_json()`'s, and no other serializer's.**" in text
    for name, model in _models().items():
        written = json.loads(json.dumps(model.to_json()))  # type: ignore[attr-defined]
        assert name.split()[0] in text, name
        # §3's names, never Python's.
        assert not any("_" in member for member in written), name
        with pytest.raises(TypeError):
            json.dumps(model)
        assert dataclasses.asdict(model) != written, name  # type: ignore[call-overload]


def test_it_names_the_commit_of_the_specification_pinned() -> None:
    """The README names the commit spec/PIN.json pins, and no other."""
    import json

    pin = json.loads((README.parent / "spec" / "PIN.json").read_text(encoding="utf-8"))
    commits = set(re.findall(r"`([0-9a-f]{40})`", _text()))
    assert commits == {pin["commit"]}


def test_it_says_to_read_the_orders_lock_with_consistent_reads() -> None:
    """SPEC.md §17: across processes, the integrator locks the order around `create_invoice`;
    in a store whose reads may lag behind its writes, the lock and what it protects are read
    with consistent reads."""
    text = _text()
    assert "lock the order in your database around `create_invoice`" in text
    assert (
        "In a store whose reads may lag behind its writes (an eventually consistent one, as some "
        "hosted platforms give), read the lock and what it protects with reads that see the "
        "latest writes (consistent reads)."
    ) in text


def test_it_documents_currency_digits_with_prices_in_a_currency() -> None:
    """PORTING.md step 7, SPEC.md §4, Functions: "Prices in a currency" names `currency_digits`,
    what it gives, and that it needs no key."""
    text = _text()
    section = text[text.index("## Prices in a currency") : text.index("## Payments and their")]
    assert "from sattleme_checkout import currency_digits" in section
    assert (
        "`currency_digits(code)` gives the decimals of the currency whose code is exactly `code` "
        "in that table, and `None` for any other text"
    ) in section
    assert "it needs no key and reads nothing" in section


def test_it_says_what_is_injected_is_called_from_several_tasks_at_once() -> None:
    """SPEC.md §4: an implementation whose calls run concurrently (tasks, here) calls what is
    injected from several of them within one call: its documentation says so, and that it must
    be safe to call so. Nothing a call started runs on once it returned (SPEC.md §13): no
    publication's relays past its majority either, whose connections the call closes; the
    threads Python cannot stop are named."""
    text = _text()
    assert (
        "**What you inject is called from several tasks at once.** A call asks the relays and the "
        "services at once, in tasks of its event loop: what you inject (`http`, `websocket`, "
        "`logger`, `now`, `random`) is called from several of them within one call"
    ) in text
    assert "and must be safe to call so" in text
    assert (
        "Once a call returned, nothing it started runs on, cancelled or not: it closed its "
        "connections, the relays of a publication past its majority dropped with them"
    ) in text
    assert (
        "a name resolution under way in the operating system ([Known limits](#known-limits)) and "
        "a custom rate source's plain function"
    ) in text
    assert "does not wait for it: it runs on in its thread until it returns" in text


def test_it_says_what_a_removed_integration_stops() -> None:
    """PORTING.md step 7, SPEC.md §14, step 3: what a removed integration stops, as
    checkout-node's README says it: the calls that read the channel; the check of an invoice
    without an address goes on, one with an address is revoked unless its Lightning invoice was
    paid; the merchant settles by hand the orders still being paid on-chain."""
    text = _text()
    assert (
        "the old key then makes no invoice (`create_invoice`), finds none (`find_invoices`) and "
        "reads no merchant (`merchant()`): `RevokedError`. A check of an invoice it made "
        "(`get_status`, `wait_for_payment`) still finds its Lightning payment: an invoice "
        "without an address is checked as before, and one with an address is `RevokedError` "
        "unless its Lightning invoice was paid (SPEC.md §14). The merchant settles by hand the "
        "orders still being paid on-chain."
    ) in text
    assert "the next invoice of any client then fails" not in text
