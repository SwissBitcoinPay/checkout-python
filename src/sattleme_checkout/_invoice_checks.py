"""The checks of an invoice the Lightning address answered (SPEC.md §18), in their order."""

from __future__ import annotations

from . import _bolt11
from ._constants import CLOCK_TOLERANCE
from ._lnurl import InvoiceAnswer, PayRequest
from ._urls import is_https_url, same_host
from .errors import InvalidInvoiceError


def check_invoice(
    answer: InvoiceAnswer, pay: PayRequest, *, sats: int, duration: int, now: int
) -> _bolt11.Bolt11:
    """The invoice decoded, once every check passed; else InvalidInvoiceError naming the check."""
    decoded = _bolt11.decode(answer.pr)
    if decoded is None:
        raise InvalidInvoiceError("Not a BOLT11 invoice", check="bolt11")
    if decoded.network != "bc":
        raise InvalidInvoiceError("Not an invoice of Bitcoin's main network", check="network")
    if decoded.amount_msat != sats * 1000:
        raise InvalidInvoiceError("The invoice's amount is not the order's", check="amount")
    if decoded.description_hash != pay.description_hash:
        raise InvalidInvoiceError(
            "The invoice does not pay the merchant's Lightning address", check="description_hash"
        )
    if not is_https_url(answer.verify) or not same_host(answer.verify, pay.callback):
        raise InvalidInvoiceError(
            "The verify URL is not an https URL of the callback's host", check="verify_url"
        )
    if decoded.expiry > duration:
        raise InvalidInvoiceError("The invoice lasts longer than asked", check="duration")
    if decoded.expiry <= 0:
        raise InvalidInvoiceError("The invoice has no time to be paid", check="expired")
    if abs(decoded.timestamp - now) > CLOCK_TOLERANCE:
        raise InvalidInvoiceError(
            "The invoice's date is more than 5 minutes off the clock: synchronize it (NTP)",
            check="clock",
        )
    if decoded.expires_at <= now:
        raise InvalidInvoiceError("The invoice has expired", check="expired")
    return decoded
