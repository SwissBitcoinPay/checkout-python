"""On-chain payments (SPEC.md §31, The on-chain payment): the statuses of the vectors.

Each case gives what the explorer, the relays and BlockCypher answer; the check's status, the
witnesses it publishes, the transactions it asks BlockCypher about and the acceptance record it
publishes are compared with the vector's. The witnesses the relays hold are sealed and signed as
an integration writes them, and opened as a check opens them; the acceptance record is read and
published by the client's own code, on local relays. The merchant's acceptances the relays hold
are sealed and signed as the merchant's app makes them, then counted as a check counts them; the
cases that have some run again end to end, read on every relay of 3 local relays.
"""

from __future__ import annotations

import logging
import random
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from fakes.http import FakeHttp
from fakes.relay import Relays
from sattleme_checkout._address import is_deposit_address
from sattleme_checkout._channel import ActiveChannel, tolerance_of
from sattleme_checkout._constants import MERCHANT_ACCEPTANCE_TYPE, WITNESS_TYPE
from sattleme_checkout._crypto import Secret, xonly_public_key
from sattleme_checkout._declaration import Registry
from sattleme_checkout._derive import (
    address_tag,
    channel_encryption_key,
    order_tag,
    rest_keys,
    seen_keys,
    witness_address,
)
from sattleme_checkout._engine import (
    Engine,
    Held,
    Parts,
    RestCheck,
    Scope,
    Settings,
    Shop,
    status_of,
)
from sattleme_checkout._explorer import Transaction, parse_transaction, received_time
from sattleme_checkout._key import IntegrationKey, parse_key
from sattleme_checkout._merchant_acceptance import (
    MerchantAcceptance,
    merchant_acceptance_filter,
    merchant_acceptance_keys,
    merchant_acceptance_of,
    open_merchant_acceptance,
    read_merchant_acceptance,
)
from sattleme_checkout._nostr import Event
from sattleme_checkout._onchain import (
    LightningResult,
    RestInvoice,
    Tolerance,
    onchain_part,
    rest_part,
)
from sattleme_checkout._records import Opened, seal
from sattleme_checkout._relay import RelayPool
from sattleme_checkout._rest import acceptance_event, open_acceptance
from sattleme_checkout._stored import InvoiceToCheck
from sattleme_checkout._witness import (
    InvoiceWitnesses,
    Witness,
    invoice_tags,
    invoice_witnesses,
    read_witness,
    witness_event,
    witness_value,
)
from sattleme_checkout.errors import (
    InvalidInvoiceError,
    NetworkError,
    SattlemeError,
)
from spec_vectors import vector

PAYMENTS: Any = vector("onchain-payments.json")
INVOICE = PAYMENTS["invoice"]
ADDRESS: str = INVOICE["onchainAddress"]
SATS = int(INVOICE["sats"])
EXPIRES_AT: int = INVOICE["expiresAt"]
SALE = "43984d278c37779e0dd825d0f34582fdbd4c2e141bbe3a3f5d1a21736c21fc7d"
# Another sale's author: its witnesses are found by the address tag.
OTHER = "ab" * 32
# Another order's address, which a transaction of the vectors pays too (a batch).
OTHER_ADDRESS = "bc1pfkumyewxhwxssrc7gw63yef3j3m93uhml8n90udtcd4nndjldydqjpmgkv"
# The integration of vectors/app/app.json, whose sale is SALE.
INTEGRATION = Secret(bytes.fromhex(vector("onchain-witnesses.json")["integrationKey"]))
SEEN = seen_keys(INTEGRATION)
CHANNEL_KEY = channel_encryption_key(INTEGRATION)
ERRORS: dict[str, type[SattlemeError]] = {
    "network": NetworkError,
    "invalid_invoice": InvalidInvoiceError,
}


# The merchant's acceptances (the team's records, SPEC.md §31, The merchant's acceptance), made by a
# team of vectors/app/merchant-acceptance.json's own (`made`: its secret is in the vector) for the
# integration key of vectors/app/app.json, so that they check out: at the address of the invoice
# of the order's sale (SALE).
MADE: Any = vector("app/merchant-acceptance.json")["made"]
TEAM_SECRET = Secret(bytes.fromhex(MADE["teamSecret"]))
MADE_KEY = parse_key(MADE["secretKey"])
ACCEPTANCE_KEYS = merchant_acceptance_keys(
    MADE_KEY.secret, MADE_KEY.team, SALE, ADDRESS, sats=SATS, expires_at=EXPIRES_AT
)
# The merchant's relays of the vector: 3, of which 2 are a majority.
MERCHANT_RELAYS = 3
NEWER_ACCEPTANCE = "merchant's acceptance of this order has a newer format"


def _acceptance_events(case: dict[str, Any]) -> list[Event]:
    """The events of a case's merchant's acceptances, sealed and signed by the team as the app
    makes them: of its `received` (a JSON number, whatever it is), or of version 2 for one of a
    newer format, dated its `createdAt`. Where the vector gives the ids of two of one date, made
    again, with other random bytes, until their ids sort as the vector's (the lowest counts)."""
    records = case.get("merchantAccepted", [])
    draw = random.Random(f"merchant's acceptance: {case['name']}")
    while True:
        events = [
            seal(
                record_type=MERCHANT_ACCEPTANCE_TYPE,
                value={"version": 2}
                if record.get("newer")
                else {"version": 1, "received": int(record["received"])},
                signer=TEAM_SECRET,
                key=ACCEPTANCE_KEYS.key,
                address=ACCEPTANCE_KEYS.address,
                created_at=record["createdAt"],
                random=draw.randbytes,
            )
            for record in records
        ]
        ids = [(record.get("id"), event.id) for record, event in zip(records, events, strict=True)]
        if all(
            (a < b) == (event_a < event_b)
            for a, event_a in ids
            for b, event_b in ids
            if a is not None and b is not None
        ):
            return events


def _held_by_relay(case: dict[str, Any]) -> list[list[Event]]:
    """What each of the 3 relays holds: each record on the first `sentBy` of them (absent: all)."""
    records = case.get("merchantAccepted", [])
    events = _acceptance_events(case)
    return [
        [
            event
            for record, event in zip(records, events, strict=True)
            if index < record.get("sentBy", MERCHANT_RELAYS)
        ]
        for index in range(MERCHANT_RELAYS)
    ]


async def _counted_read(case: dict[str, Any]) -> MerchantAcceptance:
    """The check's read of the merchant's acceptance, without the relays: each relay answers with
    what it holds, each event opened as the read keeps it (`open_merchant_acceptance`), then
    counted (`merchant_acceptance_of`); "unreadable": fewer than a majority answered."""
    if case.get("merchantAccepted") == "unreadable":
        raise NetworkError("Fewer than a majority of the relays answered (vector)")
    answers = [
        [
            opened
            for event in held
            if (opened := open_merchant_acceptance(event.to_json(), ACCEPTANCE_KEYS)) is not None
        ]
        for held in _held_by_relay(case)
    ]
    return merchant_acceptance_of(answers, MERCHANT_RELAYS)


class Warnings:
    """A logger that keeps the warnings."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, msg: str, /, *args: object) -> None:
        self.messages.append(msg % args if args else msg)


def _error(code: str) -> SattlemeError:
    return ERRORS[code](f"{code} (vector)")


class FakeIO:
    def __init__(
        self,
        case: dict[str, Any],
        read: Callable[[dict[str, Any]], Awaitable[MerchantAcceptance]] = _counted_read,
    ) -> None:
        self.case = case
        self.published: list[dict[str, Any]] = []
        self.sent: list[Witness] = []
        self.asked: list[str] = []
        self._read = read
        # Whether the check read the merchant's acceptance (the read ended).
        self.read_acceptance = False

    async def transactions(self) -> tuple[int, list[Transaction]]:
        explorer = self.case["explorer"]
        if "error" in explorer:
            raise NetworkError("explorer (vector)")
        txs = [parse_transaction(tx, ADDRESS) for tx in explorer["txs"]]
        return explorer["height"], [tx for tx in txs if tx.pays > 0]

    async def merchant_acceptance(self) -> MerchantAcceptance:
        accepted = await self._read(self.case)
        self.read_acceptance = True
        return accepted

    def witness_address(self, txid: str, sale: str | None) -> str:
        return witness_address(SEEN, ADDRESS, txid, sale)

    def _event(self, w: dict[str, Any]) -> dict[str, Any]:
        """A witness the relays hold, as an integration seals and signs it: at another address
        (`address` "other"), one a relay sends this read anyway."""
        address = OTHER_ADDRESS if w.get("address") == "other" else ADDRESS
        if w["sale"] in ("newer", "newer none"):
            # Of a newer format, at the witness address of its txid and the order's sale, or of
            # its txid and `none`, with the order tag of that address and sale or `none`.
            of = SALE if w["sale"] == "newer" else None
            return seal(
                record_type=WITNESS_TYPE,
                value={"version": 2},
                signer=SEEN.secret,
                key=CHANNEL_KEY,
                address=witness_address(SEEN, address, w["txid"], of),
                created_at=self.case["now"],
                random=secrets.token_bytes,
                tags=[["o", order_tag(SEEN, address, of)]],
            ).to_json()
        sale = {"this": SALE, "other": OTHER}.get(w["sale"])
        expires_at = w.get("expiresAt")
        if w["sale"] == "other" and expires_at is None:
            # As the vector says: both `o` tags and an `expiresAt` of that sale's invoice, which
            # nothing here depends on.
            expires_at = EXPIRES_AT
        witness = Witness(
            address=address,
            txid=w["txid"],
            sale=sale,
            sats=w["sats"],
            inputs=tuple(w["inputs"]),
            replacement=w["replacement"],
            seen_at=w["seenAt"],
            expires_at=expires_at,
        )
        return seal(
            record_type=WITNESS_TYPE,
            value=witness_value(witness),
            signer=SEEN.secret,
            key=CHANNEL_KEY,
            address=witness_address(SEEN, address, w["txid"], sale),
            created_at=w["seenAt"],
            random=secrets.token_bytes,
            tags=[["o", order_tag(SEEN, address, sale)], ["o", address_tag(SEEN, address)]],
        ).to_json()

    async def witnesses(self, *, again: bool = False) -> InvoiceWitnesses:
        found = (
            self.case.get("witnessesAgain", self.case["witnesses"])
            if again
            else (self.case["witnesses"])
        )
        if found == "unreadable":
            raise NetworkError("relays (vector)")
        values = [self._event(w) for w in found]
        if again and "witnessesAgain" not in self.case:
            # Those of `witnesses` and the ones it published.
            values += [
                witness_event(SEEN, CHANNEL_KEY, w, secrets.token_bytes).to_json()
                for w in self.sent
            ]
        # What the read keeps of each event, as the relays send it.
        tags = invoice_tags(SEEN, ADDRESS, SALE)
        kept = [read_witness(value, SEEN, CHANNEL_KEY, ADDRESS, tags) for value in values]
        return invoice_witnesses([w for w in kept if w is not None], SALE, EXPIRES_AT)

    async def publish_witness(self, witness: Witness) -> None:
        assert witness.address == ADDRESS
        assert witness.expires_at == (EXPIRES_AT if witness.sale is not None else None)
        if self.case["publishFails"]:
            raise NetworkError("publication (vector)")
        self.sent.append(witness)
        self.published.append(
            {
                "txid": witness.txid,
                "sale": "this" if witness.sale == SALE else None,
                "replacement": witness.replacement,
            }
        )

    async def received(self, txid: str) -> int | None:
        self.asked.append(txid)
        answers = self.case["blockcypher"]
        if "error" in answers:
            raise NetworkError("BlockCypher (vector)")
        if txid not in answers:
            return None
        return received_time(answers[txid])


def test_the_other_address_is_a_deposit_address() -> None:
    assert is_deposit_address(OTHER_ADDRESS)
    assert OTHER_ADDRESS != ADDRESS


def _lightning(value: dict[str, Any] | None) -> LightningResult | SattlemeError | None:
    if value is None:
        return None
    if "error" in value:
        return _error(value["error"])
    return LightningResult(value["state"], value.get("preimage"))


def _rests(case: dict[str, Any]) -> tuple[list[RestInvoice], list[Any]]:
    rests = sorted(case.get("rests", []), key=lambda r: (r["createdAt"], r["paymentHash"]))
    invoices = [
        RestInvoice(
            bolt11=r["invoice"],
            payment_hash=r["paymentHash"],
            verify_url="https://breez.tips/lnurlp/verify/" + r["paymentHash"],
            sats=int(r["sats"]),
            created_at=r["createdAt"],
            expires_at=r["expiresAt"],
        )
        for r in rests
    ]
    return invoices, [_lightning(r["lightning"]) for r in rests]


# The invoice checked, as `get_status` holds it: of an invoice with an address, the status reads
# its address, `sats` and `expiresAt` (its Lightning part is the case's `lightning`).
STORED = InvoiceToCheck(
    ref="ORD-1",
    bolt11=None,
    payment_hash=None,
    verify_url=None,
    onchain_address=ADDRESS,
    sats=SATS,
    expires_at=EXPIRES_AT,
)


async def _status(case: dict[str, Any], io: FakeIO, logger: Warnings) -> tuple[Any, Parts]:
    """The status a check gives (`status_of`, as `get_status`), from its parts: the on-chain
    part checked here, judged with the order's tolerance as its lease gives it and the
    merchant's acceptance the relays hold; and the parts."""
    invoices, results = _rests(case)
    try:
        onchain: Any = await onchain_part(
            io,
            address=ADDRESS,
            sats=SATS,
            expires_at=EXPIRES_AT,
            sale=SALE,
            clock=lambda: case["now"],
            paid_by_rests=rest_part(invoices, results).paid,
            tolerance=Tolerance(tolerance_of(case.get("tolerance"))),
            logger=logger,
        )
    except SattlemeError as error:
        onchain = error
    parts = Parts(
        lightning=_lightning(case["lightning"]),
        onchain=onchain,
        rests=RestCheck(invoices=tuple(invoices), results=tuple(results)),
    )
    try:
        status = status_of(STORED, parts, case["now"])
    except SattlemeError as error:
        return {"error": error.code}, parts
    return status.to_json(), parts


# The integration's order of the invoice, and the team that wrote its channel: the rest keys of
# the acceptance records are derived from the integration key, the order's ref, the address and
# the invoice's end (SPEC.md §31, Rest records).
TEAM = xonly_public_key(Secret(bytes([7]) * 32)).hex()
REST_KEYS = rest_keys(INTEGRATION, "ORD-1", ADDRESS, EXPIRES_AT)


async def _acceptance_published(case: dict[str, Any], parts: Parts) -> tuple[int | None, int]:
    """What the check publishes of an acceptance record when its parts are `parts` (SPEC.md §31,
    Acceptance records), as the client does (`Engine._publish_acceptance`), on three local relays
    each of which holds the record of the case's `accepted` (absent: none holds one), so that
    every majority that answers reads it: the `received` of the record the relays took (None
    when none took one), and how many of the relays were asked for the records they hold."""
    relays = Relays()
    urls = tuple(await relays.start("relay-a.example", "relay-b.example", "relay-c.example"))
    try:
        if "accepted" in case:
            held = acceptance_event(
                REST_KEYS,
                received=int(case["accepted"]),
                expires_at=EXPIRES_AT,
                created_at=case["now"] - 60,
                random=secrets.token_bytes,
            )
            for url in urls:
                relays[url].store(held)
        engine = Engine(
            Settings(
                key=IntegrationKey(TEAM, INTEGRATION, urls),
                rate_sources=(),
                coingecko_api_key=None,
                transport=FakeHttp(),
                connect=relays.connect,
                relay_timeout_ms=2000,
                http_timeout_ms=2000,
                logger=logging.getLogger("test"),
                now=lambda: int(case["now"]),
                random=secrets.token_bytes,
            )
        )
        registry = Registry(key=Secret(bytes(32)), reader=TEAM)
        shop = Shop(
            channel=ActiveChannel(
                business_name="Sunrise Bakery",
                lightning_address="shop@breez.tips",
                registry_key=registry.key,
                registry_reader=registry.reader,
                relays=None,
                checkout_origin=None,
                tolerance=0,
                updated_at=0,
            ),
            relays=urls,
            registry=registry,
        )
        async with engine.pool() as pool:
            await engine._publish_acceptance(
                Scope(pool, shop), Held(stored=STORED, ref="ORD-1", address=ADDRESS), parts
            )
        published: set[int] = set()
        for relay in relays.relays.values():
            for value in relay.received:
                opened = open_acceptance(value, REST_KEYS, SATS)
                assert isinstance(opened, Opened)
                published.add(opened.value)
        assert len(published) <= 1
        asked = sum(1 for relay in relays.relays.values() if relay.requests)
        return next(iter(published), None), asked
    finally:
        await relays.close()


def test_the_merchant_s_acceptances_are_made_for_the_vectors_integration() -> None:
    """`made`'s team, with the integration key of app.json (and onchain-witnesses.json): a record
    it makes checks out, at the merchant's acceptance address of the invoice of SALE."""
    assert MADE_KEY.secret.reveal() == INTEGRATION.reveal()
    assert xonly_public_key(TEAM_SECRET).hex() == MADE_KEY.team
    case = {"name": "check", "merchantAccepted": [{"received": "30000", "createdAt": EXPIRES_AT}]}
    [event] = _acceptance_events(case)
    opened = open_merchant_acceptance(event.to_json(), ACCEPTANCE_KEYS)
    assert isinstance(opened, Opened)
    assert opened.value == 30_000


async def _check(
    case: dict[str, Any],
    read: Callable[[dict[str, Any]], Awaitable[MerchantAcceptance]] = _counted_read,
) -> None:
    """One case through the package's payment check (`onchain_part`, `status_of`), with `read`
    as its read of the merchant's acceptance; then the acceptance record it publishes."""
    io = FakeIO(case, read)
    logger = Warnings()
    found, parts = await _status(case, io, logger)
    assert found == case["expected"]
    # A merchant's acceptance of a newer format is warned of once when the check read it,
    # whatever its date and however many relays sent it.
    accepted = case.get("merchantAccepted")
    newer = isinstance(accepted, list) and any(record.get("newer") for record in accepted)
    warned = [message for message in logger.messages if NEWER_ACCEPTANCE in message]
    assert len(warned) == (1 if newer and io.read_acceptance else 0)
    if "error" in case["expected"]:
        # What it published before failing is not checked.
        assert case["published"] == []
    else:
        assert io.published == case["published"]
    assert io.asked == case["asked"]
    expected = case.get("acceptance")
    acceptance, asked = await _acceptance_published(case, parts)
    assert acceptance == (None if expected is None else int(expected))
    if "accepted" in case or expected is not None:
        # The records the relays hold were read: a majority answered.
        assert asked >= 2


@pytest.mark.parametrize("case", PAYMENTS["cases"], ids=lambda c: c["name"])
async def test_onchain_payment(case: dict[str, Any]) -> None:
    await _check(case)


@pytest.mark.parametrize(
    "case", [c for c in PAYMENTS["cases"] if "merchantAccepted" in c], ids=lambda c: c["name"]
)
async def test_onchain_payment_end_to_end_on_3_local_relays(case: dict[str, Any]) -> None:
    """The cases with merchant's acceptances again, the check reading them on every relay of 3
    local relays (`read_merchant_acceptance`), each holding what `_held_by_relay` says, stored as
    it is (several at one address). The first, which holds every record, answers late: a read
    that went on with the first majority to answer would miss what it holds. "unreadable": the 2
    others never answer the read."""
    relays = Relays()
    urls = await relays.start("merchant-1.example", "merchant-2.example", "merchant-3.example")
    try:
        unreadable = case["merchantAccepted"] == "unreadable"
        held = [[], [], []] if unreadable else _held_by_relay(case)
        for index, url in enumerate(urls):
            relay = relays[url]
            for event in held[index]:
                relay.hold(event)
            if index == 0:
                relay.late, relay.late_by = (lambda _filter: True), 0.2
            elif unreadable:
                relay.stalls = lambda _filter: True
        async with RelayPool(
            connect=relays.connect,
            timeout_ms=2000,
            random=secrets.token_bytes,
            now=lambda: int(case["now"]),
        ) as pool:

            async def read(_case: dict[str, Any]) -> MerchantAcceptance:
                return await read_merchant_acceptance(pool, urls, ACCEPTANCE_KEYS)

            await _check(case, read)
        # Nothing published there: a check never publishes a merchant's acceptance, nor again.
        assert [relays[url].received for url in urls] == [[], [], []]
        # Read on every relay, as the check reads it: each relay asked once, at its address alone.
        requests = [relays[url].requests for url in urls]
        if any(requests):
            assert requests == [[merchant_acceptance_filter(ACCEPTANCE_KEYS)]] * 3
    finally:
        await relays.close()
