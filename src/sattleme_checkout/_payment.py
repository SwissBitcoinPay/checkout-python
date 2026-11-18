"""The Lightning part of a payment check (SPEC.md §22): LUD-21 on the invoice's verify URL."""

from __future__ import annotations

import re

from ._constants import GRACE_SECONDS
from ._crypto import sha256
from ._http import Http
from ._lnurl import Refusal, classify
from ._onchain import LightningResult
from ._text import ascii_lower
from .errors import InvalidInvoiceError, NetworkError

_PREIMAGE = re.compile("[0-9a-fA-F]{64}")


def read_verify_answer(
    status: int, body: object, *, bolt11: str, payment_hash: str, expires_at: int, now: int
) -> LightningResult:
    """A LUD-21 answer about an invoice, at `now` (the table of SPEC.md §22)."""
    final = now >= expires_at + GRACE_SECONDS
    answer = classify(status, body)
    if isinstance(answer, Refusal):
        if answer.reason == "Not found" or not final:
            return LightningResult("expired" if final else "open")
        raise NetworkError("The Lightning address refused the payment check")
    pr = answer.get("pr")
    if not isinstance(pr, str):
        raise InvalidInvoiceError("The payment answer names no invoice")
    if ascii_lower(pr) != ascii_lower(bolt11):
        raise InvalidInvoiceError("The payment answer is about another invoice")
    if answer.get("settled") is not True:
        return LightningResult("expired" if final else "open")
    preimage = answer.get("preimage")
    if not isinstance(preimage, str) or _PREIMAGE.fullmatch(preimage) is None:
        raise InvalidInvoiceError("The payment answer's preimage is not 32 bytes of hex")
    if sha256(bytes.fromhex(preimage)).hex() != payment_hash:
        raise InvalidInvoiceError("The payment answer's preimage is not this invoice's")
    return LightningResult("paid", ascii_lower(preimage))


async def lightning_part(
    http: Http, *, bolt11: str, payment_hash: str, verify_url: str, expires_at: int, now: int
) -> LightningResult:
    """GET the verify URL and read its answer."""
    status, body = await http.get_json(verify_url)
    return read_verify_answer(
        status, body, bolt11=bolt11, payment_hash=payment_hash, expires_at=expires_at, now=now
    )
