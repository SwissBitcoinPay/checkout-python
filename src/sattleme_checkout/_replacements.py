"""The parts of replaced transactions (SPEC.md §31, Replacements).

A replacement keeps the time of the payment it replaces, for at most what that payment paid the
address; what it pays more counts at its own time. Two transactions that spend a common coin are
alternatives: at most one of them is ever mined.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Payment:
    """A transaction paying the address: what it pays, its coins, when it was seen."""

    txid: str
    sats: int
    coins: tuple[str, ...]
    order: int | None
    listed: bool


@dataclass(frozen=True, slots=True)
class Part:
    """A share of what a transaction pays, counted as `root` would count."""

    sats: int
    root: str


def parts(payments: Sequence[Payment]) -> dict[str, list[Part]]:
    """The parts of each transaction the explorer lists, by txid."""
    unique: dict[str, Payment] = {}
    for payment in payments:
        unique.setdefault(payment.txid, payment)
    ordered = sorted(
        unique.values(),
        key=lambda p: (p.order is None, 0 if p.order is None else p.order, p.txid),
    )
    position = {payment.txid: index for index, payment in enumerate(ordered)}
    roots: dict[str, list[str]] = {}
    # The roots of the transactions so far that spend each coin: the roots of a transaction are
    # those of its coins (each once, in the order of step 1), in one pass over the list.
    of_coin: dict[str, set[str]] = {}
    for payment in ordered:
        found: set[str] = set()
        for coin in payment.coins:
            found |= of_coin.get(coin, set())
        own = sorted(found, key=position.__getitem__) or [payment.txid]
        roots[payment.txid] = own
        for coin in payment.coins:
            of_coin.setdefault(coin, set()).update(own)
    to_give = {payment.txid: payment.sats for payment in ordered}
    result: dict[str, list[Part]] = {}
    for payment in ordered:
        if not payment.listed:
            continue
        own_roots = roots[payment.txid]
        if own_roots == [payment.txid]:
            result[payment.txid] = [Part(payment.sats, payment.txid)]
            to_give[payment.txid] = 0
            continue
        left = payment.sats
        shares: list[Part] = []
        for root in own_roots:
            share = min(left, to_give[root])
            if share > 0:
                shares.append(Part(share, root))
                left -= share
                to_give[root] -= share
        if left > 0:
            shares.append(Part(left, payment.txid))
        result[payment.txid] = shares
    return result
