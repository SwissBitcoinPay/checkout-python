"""A fake Lightning address (LUD-06, LUD-12, LUD-16, LUD-21), as breez.tips answers: never the
network, never real funds. Its invoices are BOLT11 invoices whose signature is zeros (SPEC.md §19:
the signature is not verified)."""

from __future__ import annotations

import hashlib
import secrets
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sattleme_checkout._bech32 import Encoding, convert_bits, encode


def _words(number: int, length: int | None = None) -> list[int]:
    words: list[int] = []
    while number:
        words.insert(0, number & 31)
        number >>= 5
    if length is not None:
        words = [0] * (length - len(words)) + words
    return words or [0]


def _field(tag: int, data: list[int]) -> list[int]:
    return [tag, len(data) >> 5, len(data) & 31, *data]


def _bytes_words(data: bytes) -> list[int]:
    words = convert_bits(list(data), 8, 5, pad=True)
    assert words is not None
    return words


def bolt11(
    *,
    msat: int | None,
    payment_hash: bytes,
    description_hash: bytes,
    timestamp: int,
    expiry: int | None,
    network: str = "bc",
    description_words: int = 0,
) -> str:
    """A BOLT11 invoice; `description_words` adds description fields (to make it long)."""
    hrp = "ln" + network
    if msat is not None:
        if msat % 100 == 0:
            hrp += f"{msat // 100}n"
        else:
            hrp += f"{msat * 10}p"
    data = _words(timestamp, 7)
    data += _field(1, _bytes_words(payment_hash))
    data += _field(23, _bytes_words(description_hash))
    if expiry is not None:
        data += _field(6, _words(expiry))
    while description_words > 0:
        chunk = min(description_words, 1023)
        data += _field(13, [0] * chunk)
        description_words -= chunk
    data += [0] * 104
    return encode(hrp, data, Encoding.BECH32)


@dataclass
class FakeInvoice:
    pr: str
    payment_hash: str
    preimage: str
    msat: int
    expiry: int
    comment: str | None
    settled: bool = False


@dataclass
class FakeLightning:
    """The merchant's Lightning address `shop@breez.tips`."""

    now: Callable[[], float]
    name: str = "shop"
    domain: str = "breez.tips"
    metadata: str = '[["text/plain","Pay Sunrise Bakery"]]'
    min_sendable: int = 1000
    max_sendable: int = 4_000_000_000
    comment_allowed: int = 100
    # Its callback: <base>/lnurlp/<name>/<callback_path>.
    callback_path: str = "callback"
    invoices: dict[str, FakeInvoice] = field(default_factory=dict)
    asked: list[dict[str, str]] = field(default_factory=list)
    # Overrides, to make it misbehave.
    pay_request: Any = None
    refuse_invoices: bool = False
    answer: Callable[[FakeInvoice], dict[str, Any]] | None = None
    verify_answer: Callable[[FakeInvoice | None], tuple[int, Any]] | None = None
    clock_skew: int = 0
    # Description fields added to each invoice, to make it long.
    description_words: int = 0

    @property
    def address(self) -> str:
        return f"{self.name}@{self.domain}"

    @property
    def base(self) -> str:
        return f"https://{self.domain}"

    def pay(self, payment_hash: str) -> None:
        self.invoices[payment_hash].settled = True

    def paid_invoices(self) -> list[FakeInvoice]:
        return [invoice for invoice in self.invoices.values() if invoice.settled]

    def handle(self, url: str) -> tuple[int, Any]:
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        if path == f"/.well-known/lnurlp/{self.name}":
            if self.pay_request is not None:
                return self.pay_request  # type: ignore[no-any-return]
            return 200, {
                "tag": "payRequest",
                "callback": f"{self.base}/lnurlp/{self.name}/{self.callback_path}",
                "minSendable": self.min_sendable,
                "maxSendable": self.max_sendable,
                "metadata": self.metadata,
                "commentAllowed": self.comment_allowed,
            }
        if path == f"/lnurlp/{self.name}/{self.callback_path}":
            query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            self.asked.append(query)
            if self.refuse_invoices:
                return 200, {"status": "ERROR", "reason": "Unavailable"}
            msat = int(query["amount"])
            expiry = int(query["expiry"])
            preimage = secrets.token_bytes(32)
            payment_hash = hashlib.sha256(preimage).digest()
            pr = bolt11(
                msat=msat,
                payment_hash=payment_hash,
                description_hash=hashlib.sha256(self.metadata.encode()).digest(),
                timestamp=int(self.now()) + self.clock_skew,
                expiry=expiry,
                description_words=self.description_words,
            )
            invoice = FakeInvoice(
                pr=pr,
                payment_hash=payment_hash.hex(),
                preimage=preimage.hex(),
                msat=msat,
                expiry=expiry,
                comment=query.get("comment"),
            )
            self.invoices[invoice.payment_hash] = invoice
            if self.answer is not None:
                return 200, self.answer(invoice)
            return 200, {
                "pr": pr,
                "verify": f"{self.base}/lnurlp/{self.name}/verify/{invoice.payment_hash}",
                "routes": [],
            }
        if path.startswith(f"/lnurlp/{self.name}/verify/"):
            found = self.invoices.get(path.rsplit("/", 1)[1])
            if self.verify_answer is not None:
                return self.verify_answer(found)
            if found is None:
                return 404, {"status": "ERROR", "reason": "Not found"}
            return 200, {
                "status": "OK",
                "settled": found.settled,
                "preimage": found.preimage if found.settled else None,
                "pr": found.pr,
            }
        return 404, {"status": "ERROR", "reason": "Unknown path"}
