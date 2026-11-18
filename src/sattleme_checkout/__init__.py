"""sattle.me checkout: Lightning and on-chain payments, from an online shop's server.

An implementation of the sattle.me checkout specification (SPEC_VERSION): with the
integration's secret key alone, a shop's server makes the invoice of each order, declares it in
the merchant's registry, and checks its payment. Start with `Client` (synchronous code) or
`AsyncClient` (asyncio).
"""

from ._http import HttpResponse, HttpTransport
from ._prices import currency_digits
from ._rates import DEFAULT_SOURCES as DEFAULT_RATE_SOURCES
from ._rates import CustomRateSource, RatePrice, RateSource
from ._relay import WebSocketConnect, WebSocketConnection
from ._version import SPEC_VERSION, __version__
from ._waiting import OnStatus
from .client import AsyncClient, Client, InvoiceLike
from .errors import (
    ChannelNotFoundError,
    CheckoutUnavailableError,
    InvalidInvoiceError,
    InvalidKeyError,
    InvalidOptionError,
    InvalidOrderError,
    InvoicePendingError,
    NetworkError,
    OnchainUnavailableError,
    OrderPaidError,
    RateError,
    RevokedError,
    SattlemeError,
    UnsupportedCurrencyError,
)
from .models import (
    DeclaredInvoice,
    Fiat,
    Invoice,
    Merchant,
    PaymentStatus,
    Price,
    RateInfo,
    Rest,
)

__all__ = [
    "DEFAULT_RATE_SOURCES",
    "SPEC_VERSION",
    "AsyncClient",
    "ChannelNotFoundError",
    "CheckoutUnavailableError",
    "Client",
    "CustomRateSource",
    "DeclaredInvoice",
    "Fiat",
    "HttpResponse",
    "HttpTransport",
    "InvalidInvoiceError",
    "InvalidKeyError",
    "InvalidOptionError",
    "InvalidOrderError",
    "Invoice",
    "InvoiceLike",
    "InvoicePendingError",
    "Merchant",
    "NetworkError",
    "OnStatus",
    "OnchainUnavailableError",
    "OrderPaidError",
    "PaymentStatus",
    "Price",
    "RateError",
    "RateInfo",
    "RatePrice",
    "RateSource",
    "Rest",
    "RevokedError",
    "SattlemeError",
    "UnsupportedCurrencyError",
    "WebSocketConnect",
    "WebSocketConnection",
    "__version__",
    "currency_digits",
]
