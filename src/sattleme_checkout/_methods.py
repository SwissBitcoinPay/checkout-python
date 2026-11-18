"""The methods of an order by its amount (SPEC.md §31, Methods by amount). Nothing is repaired:
an amount is never split between the two methods, nor moved into the sendable sats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ._constants import ONCHAIN_FLOOR
from .errors import InvalidOrderError, NetworkError, OnchainUnavailableError

Leasing = Literal["leased", "none", "explorer_failed"]
"""What leasing an address gave: an address, none, or no answer of the explorer."""


@dataclass(frozen=True, slots=True)
class Methods:
    lightning: bool
    onchain: bool


def methods_by_amount(sats: int, min_sats: int, max_sats: int, *, param: str) -> Methods:
    """Lightning within the sendable sats; on-chain allowed from 1 000 sats (an address yet to
    lease). InvalidOrderError naming `param` (`sats` or `fiat`) when neither."""
    lightning = min_sats <= sats <= max_sats
    onchain = sats >= ONCHAIN_FLOOR
    if not lightning and not onchain:
        raise InvalidOrderError(
            f"{param}: {sats} sats is below {ONCHAIN_FLOOR} sats (on-chain) and outside the "
            f"Lightning address's sendable sats ({min_sats} to {max_sats})",
            param=param,
        )
    return Methods(lightning=lightning, onchain=onchain)


def methods_after_leasing(methods: Methods, leasing: Leasing) -> Methods:
    """The methods once leasing an address gave `leasing`; an order with none fails."""
    if leasing == "leased":
        return methods
    if methods.lightning:
        return Methods(lightning=True, onchain=False)
    if leasing == "none":
        raise OnchainUnavailableError(
            "The order can only be paid on-chain, and no deposit address of the integration's lot "
            "could be leased: the merchant's app fills the lot again when it is opened"
        )
    raise NetworkError("The explorer did not answer: no deposit address could be leased")
