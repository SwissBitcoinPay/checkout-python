"""The on-chain payment of an invoice with an address (SPEC.md §31, The on-chain payment), and
how the parts of an invoice combine into its status.

The algorithm reads and writes through `OnchainIO` (the explorer, the witnesses and the
merchant's acceptance on the relays, BlockCypher), so that it runs the same against the network
and against the vectors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ._address import payment_uri
from ._constants import CHECKOUT_LIFE, CONFIRMATIONS, GRACE_SECONDS, ONCHAIN_FLOOR
from ._explorer import Transaction
from ._log import Logger
from ._merchant_acceptance import MerchantAcceptance
from ._replacements import Part, Payment, parts
from ._tasks import both
from ._witness import InvoiceWitnesses, Witness
from .errors import SattlemeError
from .models import PaymentStatus, Rest

_NEWER_WITNESS = (
    "A witness of this address has a newer format than this version of sattleme-checkout "
    "reads: update it"
)
_NEWER_MERCHANT_ACCEPTANCE = (
    "A merchant's acceptance of this order has a newer format than this version of "
    "sattleme-checkout reads: update it"
)


async def _value(value: int | Awaitable[int]) -> int:
    return value if isinstance(value, int) else await value


@dataclass(frozen=True, slots=True)
class Tolerance:
    """The order's tolerance (basis points), which never shows: SPEC.md §27 keeps it out of an
    error and a log, where an error's frames (a task's result among them) would carry it."""

    basis_points: int = field(repr=False)


async def _tolerance(value: Tolerance | Awaitable[Tolerance]) -> Tolerance:
    return value if isinstance(value, Tolerance) else await value


@dataclass(frozen=True, slots=True)
class _Due:
    """What is due on-chain, which gives the tolerance away: never shown either."""

    sats: int = field(repr=False)


def least_accepted(sats: int, tolerance: int) -> int:
    """What the payments in time must reach with the merchant's tolerance (basis points)."""
    return sats - sats * tolerance // 10_000


def due_onchain(
    sats: int, tolerance: int, paid_by_rests: int, merchant_accepted: int | None = None
) -> int:
    """What is due on-chain (SPEC.md §31, The on-chain payment, step 4): 0 once the rest invoices
    paid `sats`; else max(1, min(L, M) − R), L the least the tolerance accepts, M the `received`
    of the merchant's acceptance that counts (`sats` without one: None), R what the rest invoices
    paid. R is taken once from the lesser: M counts what they had paid when the merchant
    accepted, as L counts every payment. At least 1 sat: something must count on-chain for the
    tolerance or the merchant's acceptance to apply."""
    if paid_by_rests >= sats:
        return 0
    least = least_accepted(sats, tolerance)
    accepted = least if merchant_accepted is None else min(least, merchant_accepted)
    return max(1, accepted - paid_by_rests)


class OnchainIO(Protocol):
    """What the on-chain part reads and writes."""

    async def transactions(self) -> tuple[int, list[Transaction]]:
        """The chain's height and the transactions paying the address (the explorer)."""
        ...

    async def witnesses(self, *, again: bool = False) -> InvoiceWitnesses:
        """The witnesses of the invoice (NetworkError when fewer than a majority answered);
        `again`: read again after this check published some, on every relay."""
        ...

    async def merchant_acceptance(self) -> MerchantAcceptance:
        """The merchant's acceptance of the invoice, read on every relay (NetworkError when fewer
        than a majority answered)."""
        ...

    def witness_address(self, txid: str, sale: str | None) -> str:
        """The witness address of the address, `txid` and a sale (None: `none`)."""
        ...

    async def publish_witness(self, witness: Witness) -> None:
        """Publish a witness on a majority (NetworkError otherwise)."""
        ...

    async def received(self, txid: str) -> int | None:
        """BlockCypher's time of a transaction (Unix ms); None when it does not know it yet."""
        ...


Timing = Literal["in_time", "replacement", "late", "undecided", "anothers"]


@dataclass(frozen=True, slots=True)
class OnchainResult:
    """The on-chain part: underpaid (final or not), unconfirmed or paid."""

    state: Literal["underpaid", "unconfirmed", "paid"]
    received: int
    final: bool = False
    confirmations: int = 0
    txids: tuple[str, ...] = ()
    counted: dict[str, int] = field(default_factory=dict)


def _parts_of(
    witnesses: InvoiceWitnesses, transactions: Sequence[Transaction]
) -> dict[str, list[Part]]:
    """The parts of the transactions paying the address (SPEC.md §31, step 2): from the
    witnesses of this invoice and of other invoices (in the order they were seen), then the
    transactions no such witness names (their order unknown). A replacement of another
    invoice's payment stands for that one."""
    listed = {tx.txid for tx in transactions}
    named = [*witnesses.of_invoice.values(), *witnesses.of_others.values()]
    witnessed = {witness.txid for witness in named}
    payments = [
        Payment(w.txid, w.sats, w.inputs, w.seen_at, listed=w.txid in listed) for w in named
    ] + [
        Payment(tx.txid, tx.pays, tx.coins, None, listed=True)
        for tx in transactions
        if tx.txid not in witnessed
    ]
    return parts(payments)


def _anothers(witnesses: InvoiceWitnesses, shares: dict[str, list[Part]], txid: str) -> bool:
    """Another invoice's (step 3.1): a witness of another invoice names it, or one of its parts
    is of a root one names. A transaction counts for one invoice at most."""
    return txid in witnesses.of_others or any(
        share.root in witnesses.of_others for share in shares.get(txid, [])
    )


async def onchain_part(
    io: OnchainIO,
    *,
    address: str,
    sats: int,
    expires_at: int,
    sale: str,
    clock: Callable[[], int],
    paid_by_rests: int | Awaitable[int],
    tolerance: Tolerance | Awaitable[Tolerance],
    logger: Logger,
) -> OnchainResult:
    """The on-chain part, checked at `now`, the `clock` once the explorer answered (SPEC.md §31,
    steps 1 to 4): a transaction it lists was broadcast by then, so one listed after the lease's
    end never counts as in time for a clock read before the answer.

    `paid_by_rests` and `tolerance` may still be on their way (the rest part runs meanwhile):
    they are awaited at step 4 only. The merchant's acceptance is read with the witnesses, when
    some transaction pays the address: what is due is judged with it too (`due_onchain`).
    """
    end = expires_at + GRACE_SECONDS
    height, transactions = await io.transactions()
    # After the explorer's answer, never before: "listed now, it was broadcast by now".
    now = clock()
    # The witnesses, and the merchant's acceptance with them (on every relay), when some
    # transaction pays the address.
    witnesses, accepted = (
        await both(io.witnesses(), io.merchant_acceptance())
        if transactions
        else (InvoiceWitnesses.none(), MerchantAcceptance())
    )
    if witnesses.newer:
        # Never used, never written over (SPEC.md §12): this version cannot read it.
        logger.warning(_NEWER_WITNESS)
    if accepted.newer:
        logger.warning(_NEWER_MERCHANT_ACCEPTANCE)
    listed = {tx.txid for tx in transactions}
    shares = _parts_of(witnesses, transactions)
    named = {io.witness_address(tx.txid, s) for tx in transactions for s in (sale, None)}
    newer_unnamed = bool(witnesses.newer - named)
    published = False

    async def publish(tx: Transaction, *, of_sale: bool, replacement: bool) -> None:
        nonlocal published
        witness = Witness(
            address=address,
            txid=tx.txid,
            sale=sale if of_sale else None,
            sats=tx.pays,
            inputs=tx.coins,
            replacement=replacement,
            seen_at=now,
            expires_at=expires_at if of_sale else None,
        )
        if of_sale:
            await io.publish_witness(witness)
            published = True
            return
        try:
            await io.publish_witness(witness)
        except SattlemeError as error:
            # A witness `none` is for the merchant's app only: its failure is logged.
            logger.warning("A witness of a late payment was not published: %s", error.message)

    timing: dict[str, Timing] = {}
    for tx in transactions:
        own = witnesses.of_invoice.get(tx.txid)
        if _anothers(witnesses, shares, tx.txid):
            # Nothing is published nor asked for it.
            timing[tx.txid] = "anothers"
        elif own is not None and not own.replacement:
            timing[tx.txid] = "in_time"
        elif io.witness_address(tx.txid, sale) in witnesses.newer:
            timing[tx.txid] = "undecided"
        elif now <= end:
            timing[tx.txid] = "in_time"
            await publish(tx, of_sale=True, replacement=False)
        elif own is not None and own.replacement:
            timing[tx.txid] = "replacement"
        else:
            tx_shares = shares.get(tx.txid, [])
            replaces = any(share.root != tx.txid for share in tx_shares)
            if all(share.root != tx.txid for share in tx_shares):
                found: Timing = "replacement"
            else:
                received_ms = await io.received(tx.txid)
                if received_ms is not None and received_ms <= end * 1000:
                    timing[tx.txid] = "in_time"
                    await publish(tx, of_sale=True, replacement=False)
                    continue
                found = "replacement" if replaces else "late"
            if newer_unnamed:
                timing[tx.txid] = "undecided"
            elif found == "replacement":
                timing[tx.txid] = "replacement"
                await publish(tx, of_sale=True, replacement=True)
            else:
                timing[tx.txid] = "late"
                # A witness `none` names it already, or one of a newer format holds its place:
                # never written over (SPEC.md §12, §31 step 6).
                none_place = io.witness_address(tx.txid, None)
                if tx.txid not in witnesses.of_none and none_place not in witnesses.newer:
                    await publish(tx, of_sale=False, replacement=False)
    if published:
        # Its witnesses on a majority, read again on every relay: of two checks that publish
        # witnesses of one transaction at once for two sales, the later reader sees both.
        again = await io.witnesses(again=True)
        if again.newer - witnesses.newer:
            logger.warning(_NEWER_WITNESS)
        again_shares = _parts_of(again, transactions)
        for tx in transactions:
            if _anothers(again, again_shares, tx.txid):
                timing[tx.txid] = "anothers"

    def root_counts(root: str) -> bool:
        if root in listed:
            return timing.get(root) == "in_time"
        own = witnesses.of_invoice.get(root)
        return own is not None and not own.replacement

    counted: dict[str, int] = {}
    for tx in transactions:
        if timing[tx.txid] in ("undecided", "anothers"):
            continue
        value = sum(share.sats for share in shares.get(tx.txid, []) if root_counts(share.root))
        if value > 0:
            counted[tx.txid] = value
    received = sum(counted.values())
    order_transactions = [tx for tx in transactions if tx.txid in counted]
    due = _Due(
        due_onchain(
            sats,
            (await _tolerance(tolerance)).basis_points,
            await _value(paid_by_rests),
            accepted.received,
        )
    )
    if due.sats == 0:
        # The rest invoices paid the order: nothing more to ask of the explorer.
        return OnchainResult("paid", received, counted=counted)
    undecided = any(value == "undecided" for value in timing.values())
    if received < due.sats:
        return OnchainResult(
            "underpaid", received, final=now > end and not undecided, counted=counted
        )
    txids = tuple(tx.txid for tx in order_transactions)
    confirmations = min((tx.confirmations(height) for tx in order_transactions), default=0)
    # 3 confirmations on the explorer read: what it alone shows (SPEC.md §31, Explorers).
    state: Literal["unconfirmed", "paid"] = (
        "paid" if confirmations >= CONFIRMATIONS else "unconfirmed"
    )
    return OnchainResult(state, received, confirmations=confirmations, txids=txids, counted=counted)


@dataclass(frozen=True, slots=True)
class LightningResult:
    """The Lightning part of one invoice: open, expired or paid (with its preimage)."""

    state: Literal["open", "expired", "paid"]
    preimage: str | None = None


@dataclass(frozen=True, slots=True)
class RestInvoice:
    """A rest invoice of an invoice: its own invoice, read from its declaration."""

    bolt11: str
    payment_hash: str
    verify_url: str
    sats: int
    created_at: int
    expires_at: int
    recorded: bool = True


@dataclass(frozen=True, slots=True)
class RestPart:
    """The rest part: what the rest invoices paid, whether all are final, the first error."""

    paid: int = 0
    final: bool = True
    preimages: tuple[str, ...] = ()
    error: SattlemeError | None = None
    paid_hashes: frozenset[str] = frozenset()


def rest_part(
    rests: Sequence[RestInvoice], results: Sequence[LightningResult | SattlemeError]
) -> RestPart:
    paid = 0
    final = True
    preimages: list[str] = []
    hashes: set[str] = set()
    error: SattlemeError | None = None
    for rest, result in zip(rests, results, strict=True):
        if isinstance(result, SattlemeError):
            error = error or result
            final = False
        elif result.state == "paid":
            paid += rest.sats
            preimages.append(result.preimage or "")
            hashes.add(rest.payment_hash)
        elif result.state == "open":
            final = False
    return RestPart(paid, final, tuple(preimages), error, frozenset(hashes))


def status_rest(
    *,
    address: str,
    sats: int,
    expires_at: int,
    received: int,
    now: int,
    rests: Sequence[RestInvoice],
    paid: frozenset[str],
) -> Rest | None:
    """The status's `rest` (SPEC.md §31, The status's rest) of an `open` status: none before
    anything came, once nothing is left (no status is open then) or past the invoice's end."""
    if received <= 0 or received >= sats or now >= expires_at:
        return None
    left = sats - received
    candidates = [
        rest
        for rest in rests
        if rest.sats == left
        and rest.payment_hash not in paid
        and now < rest.expires_at
        and rest.recorded
    ]
    latest = max(candidates, key=lambda rest: (rest.created_at, rest.payment_hash), default=None)
    bolt11 = None if latest is None else latest.bolt11
    if left >= ONCHAIN_FLOOR:
        uri: str | None = payment_uri(address, left, bolt11)
    else:
        uri = None if bolt11 is None else f"lightning:{bolt11}"
    return Rest(sats=left, bolt11=bolt11, payment_uri=uri)


def combine(
    *,
    sats: int,
    expires_at: int,
    now: int,
    lightning: LightningResult | SattlemeError | None,
    rest: RestPart,
    onchain: OnchainResult | SattlemeError,
) -> PaymentStatus:
    """The status of an invoice with an address: its parts combined (SPEC.md §31).

    An `open` status has no `rest` yet: `status_rest` gives it.
    """
    if isinstance(lightning, LightningResult) and lightning.state == "paid":
        return PaymentStatus.paid_lightning(lightning.preimage or "")
    onchain_received = onchain.received if isinstance(onchain, OnchainResult) else 0
    received = onchain_received + rest.paid
    if rest.paid >= sats:
        return PaymentStatus.paid_mixed(received, (), rest.preimages)
    if isinstance(onchain, OnchainResult) and onchain.state == "paid":
        if rest.paid == 0:
            return PaymentStatus.paid_onchain(received, onchain.txids)
        return PaymentStatus.paid_mixed(received, onchain.txids, rest.preimages)
    if isinstance(onchain, OnchainResult) and onchain.state == "unconfirmed":
        return PaymentStatus.unconfirmed(received, onchain.confirmations, onchain.txids)
    for error in (lightning, rest.error, onchain):
        if isinstance(error, SattlemeError):
            raise error
    lightning_final = lightning is None or (
        isinstance(lightning, LightningResult) and lightning.state == "expired"
    )
    onchain_final = isinstance(onchain, OnchainResult) and onchain.final
    if lightning_final and onchain_final and rest.final:
        return PaymentStatus.expired(received)
    return PaymentStatus.open(expires_at, received, None)


def acceptance_received(
    status: PaymentStatus, *, sats: int, expires_at: int, now: int
) -> int | None:
    """The `received` of the acceptance record a check publishes, when its status calls for one:
    accepted below the amount (the tolerance, or the merchant's acceptance), on-chain, within its
    checkout links' life."""
    onchain = status.state == "unconfirmed" or (status.state == "paid" and bool(status.txids))
    if not onchain or status.received is None or status.received >= sats:
        return None
    return status.received if now < expires_at + CHECKOUT_LIFE else None
