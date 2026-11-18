"""The merchant's acceptance (SPEC.md §31, The merchant's acceptance), end to end against local
relays and fakes of the Lightning address and of the explorer (PORTING.md, Steps, 5): the team's
record, read on every relay with the witnesses, pays an order underpaid on-chain down to what the
merchant accepted, only when a majority of the merchant's relays hold it, whichever majority
answers; the pages of the order's checkout links told by an acceptance record (the server's); the
check publishing nothing of it."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from fakes.chain import deposit_address, tx, txid
from fakes.lightning import FakeInvoice
from fakes.wait import until
from fakes.world import World
from sattleme_checkout import AsyncClient, NetworkError, OrderPaidError, PaymentStatus
from sattleme_checkout._constants import MERCHANT_ACCEPTANCE_TYPE
from sattleme_checkout._crypto import Secret
from sattleme_checkout._derive import (
    channel_encryption_key,
    merchant_acceptance_address,
    rest_keys,
    sale_keys,
)
from sattleme_checkout._merchant_acceptance import (
    MerchantAcceptance,
    merchant_acceptance_keys,
    merchant_acceptance_of,
    open_merchant_acceptance,
)
from sattleme_checkout._nostr import Event, sign_event
from sattleme_checkout._records import Opened, seal
from sattleme_checkout._relay import fresh_key
from sattleme_checkout._rest import open_acceptance
from sattleme_checkout.models import Invoice

SATS = 49_000
# What came on-chain, and what the merchant accepted.
CAME = 30_000
NEWER_WARNING = "merchant's acceptance of this order has a newer format"


@dataclass
class Scene:
    """An order of 49 000 sats with an address, a minute old, 30 000 paid to it in time."""

    world: World
    client: AsyncClient
    invoice: Invoice
    address: str
    paying: str

    @property
    def urls(self) -> list[str]:
        return self.world.relay_urls

    def pay(self, sats: int, inputs: list[str] | None = None) -> str:
        """A transaction paying the address `sats`, listed by the explorer."""
        paying = txid()
        transaction = tx(paying, {self.address: sats}, inputs=inputs)
        self.world.explorer.add(self.address, transaction)
        return paying

    def drop(self, paying: str) -> None:
        self.world.explorer.drop(self.address, paying)

    def confirm(self, paying: str) -> None:
        self.world.explorer.confirm(paying, self.world.explorer.height - 2)

    def acceptance_address(
        self, *, ref: str = "R-1", address: str | None = None, expires_at: int | None = None
    ) -> str:
        """Where the merchant's acceptance of the invoice is (or of what is given instead)."""
        return merchant_acceptance_address(
            self.world.merchant.key.secret,
            sale_keys(self.world.merchant.key.secret, ref).author,
            self.address if address is None else address,
            self.invoice.expires_at if expires_at is None else expires_at,
        )

    def acceptance(
        self,
        received: int,
        *,
        created_at: int | None = None,
        signer: Secret | None = None,
        value: Any = None,
        of: dict[str, Any] | None = None,
    ) -> Event:
        """The merchant's acceptance of the invoice at `received`, as the merchant's app makes it:
        sealed with the channel's key, signed by the team (or `signer`), dated `created_at` (a
        minute ago by default), of `value` when given."""
        merchant = self.world.merchant
        return seal(
            record_type=MERCHANT_ACCEPTANCE_TYPE,
            value={"version": 1, "received": received} if value is None else value,
            signer=merchant.team_secret if signer is None else signer,
            key=channel_encryption_key(merchant.key.secret),
            address=self.acceptance_address(**(of or {})),
            created_at=int(self.world.now) - 60 if created_at is None else created_at,
            random=secrets.token_bytes,
        )

    def hold(self, event: Event, indexes: tuple[int, ...] = (0, 1, 2)) -> None:
        """The relays at `indexes` take `event`, as a relay keeps it."""
        for index in indexes:
            self.world.relays[self.urls[index]].store(event)

    def mute_read(self, indexes: tuple[int, ...]) -> None:
        """The relays at `indexes` never answer the read of the merchant's acceptance."""
        address = self.acceptance_address()

        def stalls(filter_: dict[str, Any]) -> bool:
            return address in filter_.get("#d", [])

        for index, url in enumerate(self.urls):
            self.world.relays[url].stalls = stalls if index in indexes else None

    def acceptance_records(self, index: int = 0) -> list[int]:
        """The acceptance records of the invoice a relay holds, as a checkout link's page opens
        them (the server's, for the pages)."""
        keys = rest_keys(
            self.world.merchant.key.secret, "R-1", self.address, self.invoice.expires_at
        )
        found = []
        for event in self.world.relays[self.urls[index]].events.values():
            if isinstance(event, Event):
                opened = open_acceptance(event.to_json(), keys, SATS)
                if isinstance(opened, Opened):
                    found.append(opened.value)
        return found

    def sent_of_the_team(self) -> list[dict[str, Any]]:
        """What the check sent the relays of the team's, or at the merchant's acceptance's
        address."""
        address = self.acceptance_address()
        return [
            value
            for url in self.urls
            for value in self.world.relays[url].received
            if value["pubkey"] == self.world.merchant.team
            or any(tag[1:2] == [address] for tag in value["tags"])
        ]

    def made_of(self, sats: int) -> list[FakeInvoice]:
        """The invoices the Lightning address made of `sats`."""
        return [i for i in self.world.lightning.invoices.values() if i.msat == sats * 1000]

    def underpaid(self) -> dict[str, Any]:
        """The status of the order with 30 000 in time and the rest asked, 19 000."""
        [rest] = self.made_of(SATS - CAME)
        return {
            "state": "open",
            "expiresAt": self.invoice.expires_at,
            "received": str(CAME),
            "rest": {
                "sats": str(SATS - CAME),
                "bolt11": rest.pr,
                "paymentUri": f"bitcoin:{self.address}?amount=0.00019&lightning={rest.pr}",
            },
        }

    def unconfirmed(self, received: int = CAME) -> dict[str, Any]:
        return {
            "state": "unconfirmed",
            "received": str(received),
            "confirmations": 0,
            "txids": [self.paying],
        }

    async def status(self) -> dict[str, Any]:
        return (await self.client.get_status(self.invoice)).to_json()

    def newer_warnings(self) -> list[str]:
        return [w for w in self.world.warnings if NEWER_WARNING in w]


@contextlib.asynccontextmanager
async def scene(*, tolerance: int | None = None, came: int = CAME) -> AsyncIterator[Scene]:
    address = deposit_address()
    world = await World.start(lot=[address], tolerance=tolerance)
    try:
        client = world.client()
        invoice = await client.create_invoice(
            ref="R-1", sats=SATS, duration_seconds=600, note="Order 1", checkout=True
        )
        assert invoice.onchain_address == address
        world.now += 60
        found = Scene(world, client, invoice, address, "")
        found.paying = found.pay(came)
        yield found
    finally:
        await world.close()


# A merchant's acceptance a majority of the relays hold.


async def test_it_pays_the_order_down_to_it_the_pages_told_nothing_published_of_it() -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME), (0, 1))
        assert await s.status() == s.unconfirmed()
        # No rest asked: the order is paid.
        assert s.made_of(SATS - CAME) == []
        # The acceptance record (the server's, for the pages of the order's checkout links), on
        # every relay: what was received, nothing of the merchant's acceptance itself.
        for index in range(3):
            assert s.acceptance_records(index) == [CAME]
        # The merchant's acceptance never published again, nor anything of the team's.
        assert s.sent_of_the_team() == []
        address = s.acceptance_address()
        assert not any(
            isinstance(e, Event) and e.tag("d") == address
            for e in s.world.relays[s.urls[2]].events.values()
        )

        s.confirm(s.paying)
        s.world.now = s.invoice.expires_at + 3600
        paid = {"state": "paid", "method": "onchain", "received": str(CAME), "txids": [s.paying]}
        assert await s.status() == paid
        with pytest.raises(OrderPaidError) as raised:
            await s.client.create_invoice(ref="R-1", sats=SATS)
        assert raised.value.status.to_json() == paid
        assert s.world.warnings == []


@pytest.mark.parametrize(
    ("holders", "failing"),
    [((0, 1), (2,)), ((1, 2), (0,)), ((0, 1), ())],
    ids=["the third failing", "the first failing, the 2 others holding it", "every relay"],
)
async def test_it_counts_whichever_majority_answers(
    holders: tuple[int, ...], failing: tuple[int, ...]
) -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME), holders)
        s.mute_read(failing)
        assert await s.status() == s.unconfirmed()


async def test_it_waits_for_a_relay_that_holds_it_and_answers_late() -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME), (0, 1))
        address = s.acceptance_address()
        late = s.world.relays[s.urls[0]]
        late.late, late.late_by = (lambda filter_: address in filter_.get("#d", [])), 0.4
        assert await s.status() == s.unconfirmed()


async def test_one_made_before_the_end_counts_after_the_lease_s_end_never_expired() -> None:
    async with scene() as s:
        assert await s.status() == s.underpaid()
        s.hold(s.acceptance(CAME, created_at=s.invoice.expires_at - 60), (1, 2))
        s.world.now = s.invoice.expires_at + 3600
        assert await s.status() == s.unconfirmed()


async def test_one_of_less_than_what_came_what_came_pays() -> None:
    async with scene() as s:
        s.hold(s.acceptance(20_000))
        assert await s.status() == s.unconfirmed()
        assert s.acceptance_records() == [CAME]


async def test_one_of_more_than_what_came_asks_the_whole_rest() -> None:
    async with scene() as s:
        s.hold(s.acceptance(35_000))
        assert await s.status() == s.underpaid()
        assert s.acceptance_records() == []


# A merchant's acceptance only a minority of the relays holds.


async def test_one_a_minority_holds_never_counts_whichever_relays_answer() -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME), (0,))
        assert await s.status() == s.underpaid()
        # The relay that holds it and one that does not answer: still a minority's.
        s.mute_read((2,))
        assert await s.status() == s.underpaid()
        s.mute_read(())
        s.world.now = s.invoice.expires_at + 60
        status = await s.status()
        assert (status["state"], status["rest"]) == ("open", None)
        s.world.now = s.invoice.expires_at + 3600
        assert await s.status() == {"state": "expired", "received": str(CAME)}
        assert s.acceptance_records() == []


async def test_a_holder_failing_while_the_other_holders_are_a_minority() -> None:
    """SPEC.md §27, the limit: that check does not count it; a later one, which that relay
    answers, does."""
    async with scene() as s:
        s.hold(s.acceptance(CAME), (0, 1))
        s.mute_read((0,))
        assert await s.status() == s.underpaid()
        s.mute_read(())
        assert await s.status() == s.unconfirmed()


async def test_a_relay_counts_once_whatever_copies_of_it_it_sends() -> None:
    async with scene() as s:
        event = s.acceptance(CAME)
        # The same event under another signature (BIP-340: any of them verifies): the same record.
        resigned = sign_event(
            s.world.merchant.team_secret,
            created_at=event.created_at,
            kind=event.kind,
            tags=event.tags,
            content=event.content,
            aux=bytes([77]) * 32,
        )
        assert (resigned.id, resigned.sig != event.sig) == (event.id, True)
        relay = s.world.relays[s.urls[0]]
        relay.store(event)
        address = s.acceptance_address()

        def copies(filter_: dict[str, Any]) -> list[dict[str, Any]]:
            if address not in filter_.get("#d", []):
                return []
            return [event.to_json(), resigned.to_json(), event.to_json()]

        relay.forge = copies
        assert await s.status() == s.underpaid()
        # The count itself, of what the relays sent (the pool already reads a relay's copies of
        # one id once): one relay, whatever copies it sent; then a second one, a majority.
        keys = merchant_acceptance_keys(
            s.world.merchant.key.secret,
            s.world.merchant.team,
            sale_keys(s.world.merchant.key.secret, "R-1").author,
            s.address,
            sats=SATS,
            expires_at=s.invoice.expires_at,
        )

        def opened(value: Event) -> Any:
            found = open_merchant_acceptance(value.to_json(), keys)
            assert isinstance(found, Opened)
            return found

        mixed = dataclasses.replace(event, sig=resigned.sig)
        answers: list[list[Any]] = [
            [opened(e) for e in (event, event, resigned, mixed)],
            [],
            [],
        ]
        assert merchant_acceptance_of(answers, 3) == MerchantAcceptance()
        answers[1] = [opened(resigned)]
        assert merchant_acceptance_of(answers, 3) == MerchantAcceptance(received=CAME)


# A record that is not this invoice's merchant's acceptance.


def _not_counted() -> list[Any]:
    def of(**changes: Any) -> Callable[[Scene], dict[str, Any]]:
        def options(s: Scene) -> dict[str, Any]:
            resolved = {k: v(s) if callable(v) else v for k, v in changes.items()}
            return {"of": resolved}

        return options

    return [
        pytest.param(
            of(expires_at=lambda s: s.invoice.expires_at + 3600),
            id="of another invoice of the order (another expiresAt)",
        ),
        pytest.param(of(ref="R-2"), id="of another order"),
        pytest.param(of(address=lambda _s: deposit_address()), id="of another address"),
        pytest.param(lambda _s: {"signer": fresh_key(secrets.token_bytes)}, id="of another team"),
        pytest.param(
            lambda _s: {"value": {"version": 1, "received": SATS}}, id="of the whole amount"
        ),
        pytest.param(lambda _s: {"value": {"version": 1, "received": 0}}, id="of 0 sats"),
        pytest.param(lambda _s: {"value": {"received": CAME}}, id="without a version"),
    ]


@pytest.mark.parametrize("options", _not_counted())
async def test_it_is_not_counted(options: Callable[[Scene], dict[str, Any]]) -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME, **options(s)))
        assert await s.status() == s.underpaid()
        assert s.world.warnings == []


async def test_it_is_not_counted_with_a_damaged_signature() -> None:
    async with scene() as s:
        event = s.acceptance(CAME)
        sig = event.sig[:-1] + ("1" if event.sig.endswith("0") else "0")
        for url in s.urls:
            s.world.relays[url].hold(dataclasses.replace(event, sig=sig))
        assert await s.status() == s.underpaid()


# Dated after now: its `created_at` is never compared with now, only with the lease's end.
@pytest.mark.parametrize(
    ("after", "counted"),
    [(120, True), (121, False)],
    ids=["at the lease's end: counted", "a second after the lease's end: not counted"],
)
async def test_it_is_dated(after: int, counted: bool) -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME, created_at=s.invoice.expires_at + after))
        assert await s.status() == (s.unconfirmed() if counted else s.underpaid())


# A merchant's acceptance of a newer format.


def _newer_with(s: Scene, created_at: int, valid_at: int) -> None:
    """A record of version 2 dated `created_at`, and a valid one dated `valid_at`, on every relay
    (each relay sends both)."""
    newer = s.acceptance(CAME, created_at=created_at, value={"version": 2})
    valid = s.acceptance(CAME, created_at=valid_at)
    for url in s.urls:
        for event in (valid, newer):
            s.world.relays[url].hold(event)


async def test_one_newer_than_a_valid_one_leaves_the_order_without_one_and_warns() -> None:
    async with scene() as s:
        _newer_with(s, int(s.world.now) - 30, int(s.world.now) - 60)
        assert await s.status() == s.underpaid()
        assert len(s.newer_warnings()) == 1


async def test_one_older_than_a_valid_one_leaves_it_counted_and_warns() -> None:
    async with scene() as s:
        _newer_with(s, int(s.world.now) - 90, int(s.world.now) - 60)
        assert await s.status() == s.unconfirmed()
        assert len(s.newer_warnings()) == 1


async def test_one_dated_after_the_lease_s_end_is_set_aside_and_warns() -> None:
    async with scene() as s:
        _newer_with(s, s.invoice.expires_at + 121, int(s.world.now) - 60)
        assert await s.status() == s.unconfirmed()
        assert len(s.newer_warnings()) == 1


# With rest invoices, the tolerance, the Lightning part.


async def test_what_rest_invoices_paid_is_taken_from_it_once_unconfirmed_then_paid_mixed() -> None:
    async with scene() as s:
        # 40 000 on-chain: a rest invoice of 9 000, paid; then the transaction replaced (RBF) by
        # one paying 21 000: 30 000 in all, 19 000 asked.
        s.drop(s.paying)
        first = s.pay(40_000)
        await s.status()
        [rest] = s.made_of(9000)
        s.world.lightning.pay(rest.payment_hash)
        s.world.now += 1
        coins = [f"{v['txid']}:{v['vout']}" for v in s.world.explorer.known[first]["vin"]]
        s.drop(first)
        bump = s.pay(21_000, inputs=coins)
        status = await s.status()
        assert (status["state"], status["received"], status["rest"]["sats"]) == (
            "open",
            "30000",
            "19000",
        )
        # The merchant accepts what came: 21 000 due on-chain (30 000 less the 9 000 of the rest).
        s.hold(s.acceptance(CAME))
        s.paying = bump
        assert await s.status() == s.unconfirmed()
        assert s.acceptance_records() == [CAME]
        s.confirm(bump)
        s.world.now = s.invoice.expires_at + 3600
        assert await s.status() == {
            "state": "paid",
            "method": "mixed",
            "received": str(CAME),
            "txids": [bump],
            "preimages": [rest.preimage],
        }


@pytest.mark.parametrize(
    ("accepted", "came"),
    [(CAME, CAME), (48_000, 46_550)],
    ids=["the merchant's acceptance's, lower", "the tolerance's, lower"],
)
async def test_with_the_tolerance_the_lower_judges(accepted: int, came: int) -> None:
    async with scene(tolerance=500, came=came) as s:
        s.hold(s.acceptance(accepted))
        assert await s.status() == s.unconfirmed(came)
        assert s.acceptance_records() == [came]


async def test_it_changes_nothing_once_the_lightning_invoice_is_paid() -> None:
    async with scene() as s:
        s.hold(s.acceptance(CAME))
        assert s.invoice.payment_hash is not None
        s.world.lightning.pay(s.invoice.payment_hash)
        status = await s.status()
        assert (status["state"], status["method"]) == ("paid", "lightning")


async def test_it_is_network_when_fewer_than_a_majority_answer_its_read() -> None:
    async with scene() as s:
        # A first check published the witness and asked the rest: the next one publishes nothing
        # before it reads (a relay that fails its read fails every later read on every relay of
        # the call too).
        assert await s.status() == s.underpaid()
        s.hold(s.acceptance(CAME))
        s.mute_read((1, 2))
        with pytest.raises(NetworkError):
            await s.status()
        # Its Lightning invoice paid: paid all the same.
        assert s.invoice.payment_hash is not None
        s.world.lightning.pay(s.invoice.payment_hash)
        status = await s.status()
        assert (status["state"], status["method"]) == ("paid", "lightning")


async def test_wait_for_payment_sees_it_at_its_next_check_of_the_onchain_part() -> None:
    """SPEC.md §31, Waiting for an on-chain payment: it comes with no message of the explorer;
    the wait sees it at its next check of the on-chain part, the rest given till then."""
    async with scene() as s:
        statuses: list[PaymentStatus] = []
        checked = f"https://mempool.space/api/address/{s.address}"

        # The full checks of the wait: each asks the explorer for the address's transactions once
        # (creating the invoice asked it too, whether the address was paid).
        before = len(s.world.http.urls(checked))

        def full_checks() -> int:
            return len(s.world.http.urls(checked)) - before

        waiting = asyncio.ensure_future(
            s.client.wait_for_payment(s.invoice, interval_ms=250, on_status=statuses.append)
        )
        try:
            # The first full check (it asks the rest), and the one the socket's connection wakes,
            # ended their reads of the merchant's acceptance: each read the records of the rest
            # invoices after it, a relay's last request of a full check.
            rest_author = rest_keys(
                s.world.merchant.key.secret, "R-1", s.address, s.invoice.expires_at
            ).author

            def rest_reads() -> int:
                return sum(
                    request.get("authors") == [rest_author] and "#d" not in request
                    for request in s.world.relays[s.urls[0]].requests
                )

            await until(lambda: rest_reads() == 2 and statuses)
            assert full_checks() == 2
            await until(lambda: statuses[-1].to_json() == s.underpaid())
            # The merchant accepts: no message of the explorer, no check of the on-chain part;
            # the checks of the Lightning invoices give the status with its rest still.
            s.hold(s.acceptance(CAME))
            checks, seen = full_checks(), len(statuses)
            await until(lambda: len(statuses) > seen + 1)
            assert full_checks() == checks
            assert [x.to_json() for x in statuses[seen:]] == [s.underpaid()] * (
                len(statuses) - seen
            )
            # The next check of the on-chain part (a block here; every 60 s at least) counts it.
            await s.world.socket.push({"block": {"height": s.world.explorer.height}})
            await until(lambda: statuses[-1].state == "unconfirmed")
            assert statuses[-1].to_json() == s.unconfirmed()
            s.confirm(s.paying)
            await s.world.socket.push({"block": {"height": s.world.explorer.height}})
            status = await asyncio.wait_for(waiting, 10)
        finally:
            waiting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiting
        assert status.to_json() == {
            "state": "paid",
            "method": "onchain",
            "received": str(CAME),
            "txids": [s.paying],
        }
        assert s.acceptance_records() == [CAME]
