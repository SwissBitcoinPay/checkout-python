"""What the client gives and takes (SPEC.md §3).

Attributes have Python names (`expires_at`); `to_json()` gives the JSON of SPEC.md §3, with its
names (`expiresAt`), which is what an integrator stores: an invoice stored by an implementation in
another language is checked by this one, and the other way round. Amounts are `int` here and
strings of digits in JSON; dates are Unix seconds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "DeclaredInvoice",
    "Fiat",
    "Invoice",
    "Merchant",
    "PaymentStatus",
    "Price",
    "RateInfo",
    "Rest",
]

State = Literal["open", "unconfirmed", "paid", "expired"]
Method = Literal["lightning", "onchain", "mixed"]


@dataclass(frozen=True, slots=True)
class Price:
    """An order's price in a currency: `Price("24.50", "CHF")`. Never a float."""

    amount: str
    currency: str


@dataclass(frozen=True, slots=True)
class Fiat:
    """An order's price in a currency, read as minor units: `amount` has exactly `digits`."""

    amount: str
    currency: str
    minor: int
    digits: int

    def to_json(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "minor": str(self.minor),
            "digits": self.digits,
        }


@dataclass(frozen=True, slots=True)
class RateInfo:
    """The rate an order's price was converted at."""

    source: str
    currency: str
    price: str
    fetched_at: int
    time: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "currency": self.currency,
            "price": self.price,
            "fetchedAt": self.fetched_at,
            "time": self.time,
        }


@dataclass(frozen=True, slots=True)
class Invoice:
    """What `create_invoice` returns: store it with the order (`to_json()`).

    The shop's page needs `payment_uri` (or `bolt11` and `onchain_address`) and `expires_at`
    only; with a checkout link, the shop sends its customer to `checkout_url` instead.
    """

    ref: str
    bolt11: str | None
    payment_hash: str | None
    verify_url: str | None
    onchain_address: str | None
    payment_uri: str
    sats: int
    fiat: Fiat | None
    rate: RateInfo | None
    note: str
    created_at: int
    expires_at: int
    checkout_url: str | None
    redirect_url: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "bolt11": self.bolt11,
            "paymentHash": self.payment_hash,
            "verifyUrl": self.verify_url,
            "onchainAddress": self.onchain_address,
            "paymentUri": self.payment_uri,
            "sats": str(self.sats),
            "fiat": None if self.fiat is None else self.fiat.to_json(),
            "rate": None if self.rate is None else self.rate.to_json(),
            "note": self.note,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "checkoutUrl": self.checkout_url,
            "redirectUrl": self.redirect_url,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Invoice:
        """An Invoice from its stored JSON; InvalidOrderError (`invoice`) when it is not one."""
        from ._stored import invoice_from_json

        return invoice_from_json(value)


@dataclass(frozen=True, slots=True)
class DeclaredInvoice:
    """An invoice as the merchant's registry holds it (`find_invoices`, errors of an order)."""

    ref: str
    bolt11: str | None
    payment_hash: str | None
    verify_url: str | None
    onchain_address: str | None
    payment_uri: str
    sats: int
    fiat: Fiat | None
    note: str
    created_at: int
    expires_at: int
    declared_at: int

    def to_json(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "bolt11": self.bolt11,
            "paymentHash": self.payment_hash,
            "verifyUrl": self.verify_url,
            "onchainAddress": self.onchain_address,
            "paymentUri": self.payment_uri,
            "sats": str(self.sats),
            "fiat": None if self.fiat is None else self.fiat.to_json(),
            "note": self.note,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "declaredAt": self.declared_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> DeclaredInvoice:
        """A DeclaredInvoice from its JSON; InvalidOrderError (`invoice`) when it is not one."""
        from ._stored import declared_from_json

        return declared_from_json(value)


@dataclass(frozen=True, slots=True)
class Rest:
    """What is left to pay of an on-chain underpayment, and how (`PaymentStatus.rest`)."""

    sats: int
    bolt11: str | None
    payment_uri: str | None

    def to_json(self) -> dict[str, Any]:
        return {"sats": str(self.sats), "bolt11": self.bolt11, "paymentUri": self.payment_uri}


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    """The status of an invoice's payment (SPEC.md §3, PaymentStatus).

    - `open`: not paid yet; `expires_at`, `received` and `rest`;
    - `unconfirmed`: enough received in time, not deep enough yet: `received`, `confirmations`,
      `txids`;
    - `paid`: `method` "lightning" (`preimage`, the proof of payment), "onchain" (`received`,
      `txids`) or "mixed" (`received`, `txids`, `preimages`);
    - `expired`: never paid, for good: `received` (above 0 for an underpayment the merchant got).

    `received` is below the amount in `unconfirmed` and `paid` (on-chain or mixed) when the
    merchant's tolerance of on-chain underpayments, or the merchant's acceptance of what came (in
    its app), accepted it: the order is paid all the same.
    """

    state: State
    method: Method | None = None
    expires_at: int | None = None
    received: int | None = None
    rest: Rest | None = None
    confirmations: int | None = None
    txids: tuple[str, ...] | None = None
    preimage: str | None = None
    preimages: tuple[str, ...] | None = None

    @property
    def final(self) -> bool:
        """Whether it is final: paid, or expired."""
        return self.state in ("paid", "expired")

    @staticmethod
    def open(expires_at: int, received: int, rest: Rest | None) -> PaymentStatus:
        return PaymentStatus("open", expires_at=expires_at, received=received, rest=rest)

    @staticmethod
    def unconfirmed(received: int, confirmations: int, txids: tuple[str, ...]) -> PaymentStatus:
        return PaymentStatus(
            "unconfirmed", received=received, confirmations=confirmations, txids=txids
        )

    @staticmethod
    def paid_lightning(preimage: str) -> PaymentStatus:
        return PaymentStatus("paid", method="lightning", preimage=preimage)

    @staticmethod
    def paid_onchain(received: int, txids: tuple[str, ...]) -> PaymentStatus:
        return PaymentStatus("paid", method="onchain", received=received, txids=txids)

    @staticmethod
    def paid_mixed(
        received: int, txids: tuple[str, ...], preimages: tuple[str, ...]
    ) -> PaymentStatus:
        return PaymentStatus(
            "paid", method="mixed", received=received, txids=txids, preimages=preimages
        )

    @staticmethod
    def expired(received: int) -> PaymentStatus:
        return PaymentStatus("expired", received=received)

    def to_json(self) -> dict[str, Any]:
        if self.state == "open":
            return {
                "state": "open",
                "expiresAt": self.expires_at,
                "received": str(self.received),
                "rest": None if self.rest is None else self.rest.to_json(),
            }
        if self.state == "unconfirmed":
            return {
                "state": "unconfirmed",
                "received": str(self.received),
                "confirmations": self.confirmations,
                "txids": list(self.txids or ()),
            }
        if self.state == "expired":
            return {"state": "expired", "received": str(self.received)}
        if self.method == "lightning":
            return {"state": "paid", "method": "lightning", "preimage": self.preimage}
        status: dict[str, Any] = {
            "state": "paid",
            "method": self.method,
            "received": str(self.received),
            "txids": list(self.txids or ()),
        }
        if self.method == "mixed":
            status["preimages"] = list(self.preimages or ())
        return status


@dataclass(frozen=True, slots=True)
class Merchant:
    """The merchant, as the integration sees it: never its Lightning address."""

    business_name: str
    min_sats: int
    max_sats: int
    onchain_addresses: int

    def to_json(self) -> dict[str, Any]:
        return {
            "businessName": self.business_name,
            "minSats": str(self.min_sats),
            "maxSats": str(self.max_sats),
            "onchainAddresses": self.onchain_addresses,
        }
