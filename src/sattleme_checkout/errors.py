"""The errors of the package (SPEC.md §5).

Every error the package raises, but for a cancellation (`asyncio.CancelledError`, or the
`TimeoutError` of a call's own timeout), is a `SattlemeError`: a stable `code`, and
`retryable` (whether the same call may succeed later, unchanged). A message never holds a key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from .models import DeclaredInvoice, PaymentStatus

__all__ = [
    "ChannelNotFoundError",
    "CheckoutUnavailableError",
    "InvalidInvoiceError",
    "InvalidKeyError",
    "InvalidOptionError",
    "InvalidOrderError",
    "InvoicePendingError",
    "NetworkError",
    "OnchainUnavailableError",
    "OrderPaidError",
    "RateError",
    "RevokedError",
    "SattlemeError",
    "UnsupportedCurrencyError",
]


def _rebuild(cls: type[SattlemeError], message: str, state: dict[str, Any]) -> Any:
    error = cls.__new__(cls)
    Exception.__init__(error, message)
    error.__dict__.update(state)
    return error


class SattlemeError(Exception):
    """The base of every error of the package."""

    code: ClassVar[str] = "error"
    retryable: bool = False

    def __init__(self, message: str) -> None:
        super().__init__(message)

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""

    def __reduce__(self) -> tuple[Any, ...]:
        # Errors cross processes (a task queue's result backend): keep them picklable.
        return (_rebuild, (type(self), self.message, dict(self.__dict__)))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r})"


class InvalidKeyError(SattlemeError):
    """The key is not an integration key (SPEC.md §8)."""

    code = "invalid_key"


class InvalidOptionError(SattlemeError):
    """An option of the client or of a call is invalid; `param` names it."""

    code = "invalid_option"

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.param = param


class InvalidOrderError(SattlemeError):
    """A parameter of the order, or a stored invoice, is invalid; `param` names it."""

    code = "invalid_order"

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.param = param


class RevokedError(SattlemeError):
    """The merchant removed the integration."""

    code = "revoked"


class ChannelNotFoundError(SattlemeError):
    """A majority of the relays answered without the channel.

    `newer_format`: a copy of a newer format was seen; this version of the package cannot read
    it (update it). Retryable unless so.
    """

    code = "channel_not_found"

    def __init__(self, message: str, *, newer_format: bool) -> None:
        super().__init__(message)
        self.newer_format = newer_format
        self.retryable = not newer_format


class NetworkError(SattlemeError):
    """A relay, the Lightning address, a rate source or an explorer did not answer usably."""

    code = "network"
    retryable = True


class InvalidInvoiceError(SattlemeError):
    """An invoice, a pay request or a payment answer failed the checks."""

    code = "invalid_invoice"

    def __init__(self, message: str, *, check: str | None = None) -> None:
        super().__init__(message)
        self.check = check


class InvoicePendingError(SattlemeError):
    """An earlier invoice of the order may still be paid: `invoice`, and `checkout_url`."""

    code = "invoice_pending"

    def __init__(self, message: str, *, invoice: DeclaredInvoice, checkout_url: str | None) -> None:
        super().__init__(message)
        self.invoice = invoice
        self.checkout_url = checkout_url


class OrderPaidError(SattlemeError):
    """An earlier invoice of the order was paid: `invoice`, its `status`, its `preimage`."""

    code = "order_paid"

    def __init__(
        self,
        message: str,
        *,
        invoice: DeclaredInvoice,
        status: PaymentStatus,
        preimage: str | None,
    ) -> None:
        super().__init__(message)
        self.invoice = invoice
        self.status = status
        self.preimage = preimage


class UnsupportedCurrencyError(SattlemeError):
    """None of the rate sources has a price in `currency`."""

    code = "unsupported_currency"

    def __init__(self, message: str, *, currency: str) -> None:
        super().__init__(message)
        self.currency = currency


class RateError(SattlemeError):
    """No usable price: invalid, too old, or a custom source failed."""

    code = "rate"
    retryable = True


class CheckoutUnavailableError(SattlemeError):
    """A checkout link was asked, and the merchant's app gives no web app for it."""

    code = "checkout_unavailable"


class OnchainUnavailableError(SattlemeError):
    """The order can only be paid on-chain, and no address of the lot could be leased."""

    code = "onchain_unavailable"
    retryable = True
