"""On-chain rules (SPEC.md §31): replacements, tolerances, methods by amount."""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from typing import Any

import pytest

from sattleme_checkout._channel import tolerance_of
from sattleme_checkout._replacements import Part, Payment, parts
from spec_vectors import vector

REPLACEMENTS: Any = vector("onchain-replacements.json")
TOLERANCES: Any = vector("onchain-tolerances.json")


@pytest.mark.parametrize("case", REPLACEMENTS["cases"], ids=lambda c: c["name"])
def test_replacements(case: dict[str, Any]) -> None:
    payments = [
        Payment(
            txid=tx["txid"],
            sats=int(tx["sats"]),
            coins=tuple(tx["inputs"]),
            order=tx["order"],
            listed=tx["listed"],
        )
        for tx in case["txs"]
    ]
    found = {
        txid: [{"sats": str(part.sats), "root": part.root} for part in shares]
        for txid, shares in parts(payments).items()
    }
    assert found == case["parts"]


def _parts_by_definition(payments: Sequence[Payment]) -> dict[str, list[tuple[int, str]]]:
    """SPEC.md §31, Replacements, steps 1 to 3, word for word (quadratic)."""
    unique: dict[str, Payment] = {}
    for payment in payments:
        unique.setdefault(payment.txid, payment)
    ordered = sorted(unique.values(), key=lambda p: (p.order is None, p.order or 0, p.txid))
    order = [payment.txid for payment in ordered]
    roots: dict[str, list[str]] = {}
    for index, payment in enumerate(ordered):
        found: list[str] = []
        for earlier in ordered[:index]:
            if set(payment.coins) & set(earlier.coins):
                found += [root for root in roots[earlier.txid] if root not in found]
        roots[payment.txid] = sorted(found, key=order.index) or [payment.txid]
    to_give = {payment.txid: payment.sats for payment in ordered}
    result: dict[str, list[tuple[int, str]]] = {}
    for payment in ordered:
        if not payment.listed:
            continue
        if roots[payment.txid] == [payment.txid]:
            result[payment.txid] = [(payment.sats, payment.txid)]
            to_give[payment.txid] = 0
            continue
        left, shares = payment.sats, []
        for root in roots[payment.txid]:
            share = min(left, to_give[root])
            if share > 0:
                shares.append((share, root))
                left -= share
                to_give[root] -= share
        if left > 0:
            shares.append((left, payment.txid))
        result[payment.txid] = shares
    return result


def test_the_parts_as_the_specification_defines_them() -> None:
    draw = random.Random(31)
    for _ in range(5000):
        coins = [f"{draw.randrange(1 << 32):064x}:{draw.randrange(3)}" for _ in range(6)]
        payments = [
            Payment(
                txid=f"{draw.randrange(12):064x}",
                sats=draw.randrange(5000),
                coins=tuple(draw.sample(coins, draw.randrange(4))),
                order=draw.choice([None, draw.randrange(5)]),
                listed=draw.random() < 0.8,
            )
            for _ in range(draw.randrange(10))
        ]
        found = {
            txid: [(share.sats, share.root) for share in shares]
            for txid, shares in parts(payments).items()
        }
        assert found == _parts_by_definition(payments)


def test_thousands_of_transactions_in_one_pass() -> None:
    """An explorer's answer of 1 MiB lists some 7 000 transactions: read in one pass."""
    payments = [
        Payment(f"{i:064x}", 1000, ((f"{i - 1:064x}" if i else "f" * 64) + ":0",), i, True)
        for i in range(7000)
    ]
    # And as many replacing one payment, each spending its coin.
    payments += [
        Payment(f"{i:064x}", 1000, ("e" * 64 + ":0",), i, True) for i in range(7000, 14000)
    ]
    started = time.monotonic()
    found = parts(payments)
    assert time.monotonic() - started < 1
    assert found[f"{6999:064x}"] == [Part(1000, f"{6999:064x}")]
    assert found[f"{7001:064x}"] == [Part(1000, f"{7001:064x}")]


@pytest.mark.parametrize("case", TOLERANCES["values"], ids=lambda c: c["name"])
def test_tolerance_values(case: dict[str, Any]) -> None:
    value = case.get("value")
    assert tolerance_of(value) == case["expected"]


LEASES: Any = vector("onchain-leases.json")
WITNESSES: Any = vector("onchain-witnesses.json")


def _leases_registry() -> Any:
    from sattleme_checkout._crypto import Secret
    from sattleme_checkout._declaration import Registry

    return Registry(
        key=Secret(bytes.fromhex(LEASES["registryKey"])), reader=LEASES["registryReader"]
    )


def _app_sale() -> Any:
    from sattleme_checkout._derive import sale_keys
    from sattleme_checkout._key import parse_key

    key = parse_key(vector("app/app.json")["secretKey"])
    return sale_keys(key.secret, vector("app/app.json")["declaration"]["ref"])


def test_lease_keys() -> None:
    from sattleme_checkout._derive import inner_key, onchain_registry_address, outer_key

    registry, sale = _leases_registry(), _app_sale()
    assert sale.author == LEASES["saleAuthor"]
    assert sale.secret.reveal().hex() == LEASES["saleSecret"]
    assert inner_key(sale.secret, registry.reader).reveal().hex() == LEASES["innerConversationKey"]
    for case in LEASES["derived"]:
        address = onchain_registry_address(registry.key, case["address"])
        assert address == case["registryAddress"]
        assert outer_key(registry.key, address, sale.author).reveal().hex() == case["outerKey"]


@pytest.mark.parametrize("case", LEASES["events"], ids=lambda c: c["name"])
def test_lease_events(case: dict[str, Any]) -> None:
    from sattleme_checkout._derive import onchain_registry_address
    from sattleme_checkout._lease import open_lease, open_lease_record, open_sale_record
    from sattleme_checkout._records import Opened

    registry, sale = _leases_registry(), _app_sale()
    registry_address = onchain_registry_address(registry.key, case["address"])
    opened_outside = open_lease(case["event"], registry=registry, registry_address=registry_address)
    record = open_lease_record(case["event"], registry=registry, registry_address=registry_address)
    if case["outside"] is None:
        assert opened_outside is None
        assert record is None
    else:
        assert opened_outside is not None
        assert record is not None
        event, outside = opened_outside
        assert (record.id, record.pubkey, record.created_at) == (
            event.id,
            event.pubkey,
            event.created_at,
        )
        # Opening it does not check its signature; the walk does, where it decides.
        assert record.signed() is case.get("signature", True)
        if case["outside"] == "newer":
            assert outside is None
            assert record.newer
        else:
            assert outside is not None
            assert not record.newer
            expected = case["outside"]
            if expected.get("withdrawn") is True:
                assert outside.withdrawn
                assert record.end is None
            else:
                assert outside.end == record.end == expected["end"]
                assert outside.sealed == expected["sealed"]
    opened = open_sale_record(
        case["event"], sale=sale, registry=registry, registry_address=registry_address
    )
    terms = opened.value.terms if isinstance(opened, Opened) else None
    if case["inside"] is None:
        assert terms is None
        return
    assert terms is not None
    inside = case["inside"]
    assert terms.address == inside["address"]
    assert terms.sats == inside["sats"]
    assert terms.invoice == inside["invoice"]
    assert terms.verify_url == inside["verifyUrl"]
    assert terms.created_at == inside["createdAt"]
    assert terms.expires_at == inside["expiresAt"]
    assert terms.price.to_json() == inside["sale"]["fiat"]
    assert terms.note == inside["sale"]["note"]
    assert terms.tolerance == case.get("tolerance", 0)
    assert terms.confirmed is case.get("confirmed", True)


def _walked(records: list[dict[str, Any]], checked: list[int]) -> list[Any]:
    """The vector's records, as opening them leaves them (one whose id is not its hash is none),
    each noting in `checked` when its signature is checked."""
    from sattleme_checkout._lease import LeaseRecord, parse_outside
    from sattleme_checkout._nostr import Event

    class Walked(LeaseRecord):
        __slots__ = ("index", "valid")
        index: int
        valid: bool

        def signed(self) -> bool:
            checked.append(self.index)
            return bool(self.valid)

    found: list[Any] = []
    for index, record in enumerate(records):
        if record.get("id") is False:
            continue
        event = Event(
            id=record.get("eventId", f"{index:064x}"),
            pubkey=record["author"],
            created_at=record["createdAt"],
            kind=30078,
            tags=(),
            content="",
            sig="",
        )
        outside = None if record["outside"] == "newer" else parse_outside(record["outside"])
        assert record["outside"] == "newer" or outside is not None
        walked = Walked(event, outside)
        walked.index = index
        walked.valid = record.get("signature", True)
        found.append(walked)
    return found


@pytest.mark.parametrize("case", LEASES["leased"], ids=lambda c: c["name"])
def test_leased(case: dict[str, Any]) -> None:
    from sattleme_checkout._lease import leased

    checked: list[int] = []
    assert leased(_walked(case["records"], checked), case["t"]) is case["expected"]
    assert sorted(checked) == case["checked"]


@pytest.mark.parametrize("case", LEASES["confirmed"], ids=lambda c: c["name"])
def test_confirmed(case: dict[str, Any]) -> None:
    """onchain-leases.json, `confirmed`: whether the read of the address's leases, after the
    lease's publication, confirms it, as `create_invoice` decides it (a read of a majority,
    `complete` or not)."""
    from sattleme_checkout._leasing import LeaseRead
    from sattleme_checkout._nostr import Event

    records = _walked(case["records"], [])
    if case["lease"] is None:
        lease = Event("f" * 64, case["sale"], case["t"], 30078, (), "", "")
    else:
        walked = next(r for r in records if r.index == case["lease"])
        lease = Event(walked.id, walked.pubkey, walked.created_at, 30078, (), "", "")
    address = "bc1p" + "q" * 58
    read = LeaseRead(records={address: records}, answered=True, complete=case["complete"])
    assert read.confirms(address, lease, case["t"]) is case["expected"]


@pytest.mark.parametrize(("other_id", "confirmed"), [("0" * 64, False), ("f" * 64, True)])
def test_a_record_of_the_lease_s_author_in_its_second(other_id: str, confirmed: bool) -> None:
    """SPEC.md §31, Leasing an address, step 3.3: no record of the lease's author there is newer
    (the highest `created_at`, then the lowest id, as relays keep them): in the lease's second,
    one of a lower id is newer, one of a higher id is not."""
    from sattleme_checkout._lease import LeaseRecord, Outside, is_confirmed
    from sattleme_checkout._nostr import Event

    author, t = "a" * 64, 1_791_378_000
    lease = Event("8" * 64, author, t, 30078, (), "", "")
    other = Event(other_id, author, t, 30078, (), "", "")
    records = [
        LeaseRecord(lease, Outside(end=t + 1000, sealed="x")),
        LeaseRecord(other, Outside(end=None, sealed=None)),
    ]
    assert is_confirmed(records, lease, t) is confirmed


@pytest.mark.parametrize(
    ("written", "confirmed"), [(None, False), (False, False), ("true", False), (True, True)]
)
def test_a_lease_is_confirmed_by_confirmed_true_alone(
    monkeypatch: pytest.MonkeyPatch, written: object, confirmed: bool
) -> None:
    """SPEC.md §31, Opening it as the sale: the lease is confirmed when `confirmed` is `true`, not
    confirmed otherwise, absent (None here) included."""
    import secrets

    from sattleme_checkout import _lease
    from sattleme_checkout._derive import onchain_registry_address
    from sattleme_checkout._records import Opened

    registry, sale = _leases_registry(), _app_sale()
    case = next(c for c in LEASES["events"] if c.get("confirmed") is True)
    registry_address = onchain_registry_address(registry.key, case["address"])
    opened = _lease.open_sale_record(
        case["event"], sale=sale, registry=registry, registry_address=registry_address
    )
    assert isinstance(opened, Opened)
    assert opened.value.terms is not None
    inside = _lease.lease_inside

    def written_inside(terms: Any, keys: Any) -> dict[str, Any]:
        value = inside(terms, keys)
        if written is None:
            del value["confirmed"]
        else:
            value["confirmed"] = written
        return value

    monkeypatch.setattr(_lease, "lease_inside", written_inside)
    event = _lease.lease_event(
        registry=registry,
        sale=sale,
        terms=opened.value.terms,
        created_at=case["event"]["created_at"],
        random=secrets.token_bytes,
    )
    assert event is not None
    again = _lease.open_sale_record(
        event.to_json(), sale=sale, registry=registry, registry_address=registry_address
    )
    assert isinstance(again, Opened)
    assert again.value.terms is not None
    assert again.value.terms.confirmed is confirmed


def _seen() -> Any:
    from sattleme_checkout._crypto import Secret
    from sattleme_checkout._derive import seen_keys

    return seen_keys(Secret(bytes.fromhex(WITNESSES["integrationKey"])))


def test_witness_keys() -> None:
    from sattleme_checkout._derive import address_tag, order_tag, witness_address

    seen = _seen()
    derived = WITNESSES["derived"]
    assert seen.secret.reveal().hex() == derived["seenSecret"]
    assert seen.author == derived["seenAuthor"]
    assert seen.address_key.reveal().hex() == derived["seenAddressKey"]
    for case in WITNESSES["addresses"]:
        assert (
            witness_address(seen, case["address"], case["txid"], case["sale"])
            == case["witnessAddress"]
        )
    for case in WITNESSES["orderTags"]:
        assert order_tag(seen, case["address"], case["sale"]) == case["orderTag"]
    for case in WITNESSES["addressTags"]:
        assert address_tag(seen, case["address"]) == case["addressTag"]


@pytest.mark.parametrize("case", WITNESSES["events"], ids=lambda c: c["name"])
def test_witness_events(case: dict[str, Any]) -> None:
    from sattleme_checkout._crypto import Secret
    from sattleme_checkout._records import Newer, Opened
    from sattleme_checkout._witness import open_witness

    opened = open_witness(
        case["event"], _seen(), Secret(bytes.fromhex(WITNESSES["channelEncryptionKey"]))
    )
    if case["value"] is None:
        assert opened is None
    elif case["value"] == "newer":
        assert isinstance(opened, Newer)
    else:
        assert isinstance(opened, Opened)
        witness = opened.value
        expected = dict(case["value"])
        # Null (none): None.
        assert witness.expires_at == expected.pop("expiresAt", None)
        assert {
            "version": 1,
            "replacement": witness.replacement,
            "address": witness.address,
            "txid": witness.txid,
            "sale": witness.sale,
            "sats": witness.sats,
            "inputs": list(witness.inputs),
        } == expected


@pytest.mark.parametrize("case", WITNESSES["events"], ids=lambda c: c["name"])
def test_the_addresses_an_event_names(case: dict[str, Any]) -> None:
    """onchain-witnesses.json, `names`: the addresses of `addressTags` an event names when
    leasing (SPEC.md §31, Leasing an address, step 3), as the read of step 2 keeps them: of the
    seen author and checking out, each address whose tag it holds as an `o` tag, whatever its
    content."""
    from sattleme_checkout._derive import address_tag
    from sattleme_checkout._witness import addresses_named

    seen = _seen()
    by_tag = {address_tag(seen, a["address"]): a["address"] for a in WITNESSES["addressTags"]}
    named = addresses_named(case["event"], seen, by_tag)
    assert sorted(named or ()) == sorted(case["names"])


@pytest.mark.parametrize(
    "case", TOLERANCES["accepted"], ids=lambda c: f"{c['sats']}@{c['tolerance']}"
)
def test_least_accepted(case: dict[str, Any]) -> None:
    from sattleme_checkout._onchain import least_accepted

    assert str(least_accepted(int(case["sats"]), case["tolerance"])) == case["expected"]


@pytest.mark.parametrize(
    "case",
    TOLERANCES["due"],
    ids=lambda c: (
        f"{c['sats']}@{c['tolerance']}+{c['paidByRests']}"
        + (f"<={c['merchantAccepted']}" if "merchantAccepted" in c else "")
    ),
)
def test_due(case: dict[str, Any]) -> None:
    from sattleme_checkout._onchain import due_onchain

    accepted = case.get("merchantAccepted")
    due = due_onchain(
        int(case["sats"]),
        case["tolerance"],
        int(case["paidByRests"]),
        None if accepted is None else int(accepted),
    )
    assert str(due) == case["expected"]


@pytest.mark.parametrize(
    "case",
    [c for c in WITNESSES["events"] if isinstance(c["value"], dict) and "expiresAt" in c["value"]],
    ids=lambda c: c["name"],
)
def test_a_witness_written_as_the_vectors(case: dict[str, Any]) -> None:
    """A witness this package writes has the vector's `d` and `o` tags, in their order (other
    tags are the vector's own), and opens to the vector's value: its `expiresAt`, its order tag
    and its address tag (SPEC.md §31)."""
    import secrets

    from sattleme_checkout._crypto import Secret
    from sattleme_checkout._records import Opened
    from sattleme_checkout._witness import open_witness, witness_event

    channel_key = Secret(bytes.fromhex(WITNESSES["channelEncryptionKey"]))
    opened = open_witness(case["event"], _seen(), channel_key)
    assert isinstance(opened, Opened)
    written = witness_event(_seen(), channel_key, opened.value, secrets.token_bytes)
    own = [tag for tag in case["event"]["tags"] if tag[0] in ("d", "o")]
    assert [list(tag) for tag in written.tags] == own
    assert written.created_at == case["event"]["created_at"]
    again = open_witness(written.to_json(), _seen(), channel_key)
    assert isinstance(again, Opened)
    assert again.value == opened.value


@pytest.mark.parametrize(
    ("body", "height"),
    [
        (b"970000", 970_000),
        # One leading byte order mark is ignored (SPEC.md §15), as `fetch` reads a body.
        (b"\xef\xbb\xbf970000", 970_000),
        (b"\xef\xbb\xbf\xef\xbb\xbf970000", None),
        (b"970000\xef\xbb\xbf", None),
        (b" 970000", None),
    ],
)
async def test_an_explorers_height(body: bytes, height: int | None) -> None:
    from sattleme_checkout import HttpResponse
    from sattleme_checkout._constants import MEMPOOL
    from sattleme_checkout._explorer import Explorer
    from sattleme_checkout._http import Http
    from sattleme_checkout.errors import NetworkError

    async def transport(url: str, **_options: object) -> HttpResponse:
        assert url == f"{MEMPOOL}/blocks/tip/height"
        return HttpResponse(200, body)

    explorer = Explorer(Http(transport, 2000), MEMPOOL)
    if height is None:
        with pytest.raises(NetworkError):
            await explorer.height()
    else:
        assert await explorer.height() == height


def _transaction(address: str, *extra: dict[str, Any]) -> dict[str, Any]:
    """An Esplora transaction paying `address` 5 sats, with `extra` outputs."""
    return {
        "txid": "ab" * 32,
        "vin": [{"txid": "cd" * 32, "vout": 0, "is_coinbase": False}],
        "vout": [{"value": 5, "scriptpubkey_address": address}, *extra],
        "status": {"confirmed": False},
    }


def test_an_explorers_output_without_an_address_pays_nothing_one_of_null_is_no_answer() -> None:
    """SPEC.md §31, Explorers: an output's `scriptpubkey_address` is a string when present.
    Absent (OP_RETURN), the output pays nothing; null is not absent (SPEC.md §2): the answer is
    no explorer's, `network`."""
    from fakes.chain import deposit_address
    from sattleme_checkout._explorer import parse_transaction
    from sattleme_checkout.errors import NetworkError

    address = deposit_address()
    op_return = {"value": 0, "scriptpubkey": "6a0b68656c6c6f", "scriptpubkey_type": "op_return"}
    assert parse_transaction(_transaction(address, op_return), address).pays == 5
    null = {"value": 1000, "scriptpubkey_address": None}
    with pytest.raises(NetworkError):
        parse_transaction(_transaction(address, null), address)


@pytest.mark.parametrize(
    ("outputs", "pays"),
    [
        ([(2_100_000_000_000_000, True)], 2_100_000_000_000_000),
        ([(2_099_999_999_999_999, True), (1, False)], 2_099_999_999_999_999),
        ([(2_099_999_999_999_999, True), (2, False)], None),
        ([(1_500_000_000_000_000, True), (1_500_000_000_000_000, True)], None),
        ([(2**53 - 1, False)], None),
    ],
    ids=[
        "every bitcoin",
        "every bitcoin, in two outputs",
        "a sat more, to another address",
        "two outputs to the address",
        "the largest safe integer",
    ],
)
def test_a_transaction_holds_every_bitcoin_at_most(
    outputs: list[tuple[int, bool]], pays: int | None
) -> None:
    """SPEC.md §31, Explorers: a transaction whose outputs together hold more than 2 100 000 000
    000 000 sats (every bitcoin there will ever be) is none: the explorer's answer is a failure
    (`network`), never a witness of such `sats`. Each output: its value, and whether it pays the
    address."""
    from fakes.chain import deposit_address
    from sattleme_checkout._explorer import parse_transaction
    from sattleme_checkout.errors import NetworkError

    address = deposit_address()
    transaction = _transaction(address)
    transaction["vout"] = [
        {"value": value, "scriptpubkey_address": address if to_it else deposit_address()}
        for value, to_it in outputs
    ]
    if pays is None:
        with pytest.raises(NetworkError):
            parse_transaction(transaction, address)
    else:
        assert parse_transaction(transaction, address).pays == pays


@pytest.mark.parametrize(
    ("written", "opens"),
    [
        ({"invoice": None, "verifyUrl": None}, True),
        ({"invoice": "absent"}, False),
        ({"verifyUrl": "absent"}, False),
        ({"invoice": "absent", "verifyUrl": "absent"}, False),
        ({"invoice": None, "verifyUrl": "absent"}, False),
    ],
    ids=["both null", "no invoice", "no verify URL", "neither", "invoice null, no verify URL"],
)
def test_a_lease_inside_without_its_invoice_members_does_not_open(
    monkeypatch: pytest.MonkeyPatch, written: dict[str, object], opens: bool
) -> None:
    """SPEC.md §31, Opening it as the sale: `invoice` and `verifyUrl` both null (an order paid
    on-chain only), or an invoice; absent is not null (SPEC.md §2): such a lease does not open."""
    import secrets

    from sattleme_checkout import _lease
    from sattleme_checkout._derive import onchain_registry_address
    from sattleme_checkout._records import Opened

    registry, sale = _leases_registry(), _app_sale()
    case = next(c for c in LEASES["events"] if c.get("confirmed") is True)
    registry_address = onchain_registry_address(registry.key, case["address"])
    opened = _lease.open_sale_record(
        case["event"], sale=sale, registry=registry, registry_address=registry_address
    )
    assert isinstance(opened, Opened)
    assert opened.value.terms is not None
    inside = _lease.lease_inside

    def written_inside(terms: Any, keys: Any) -> dict[str, Any]:
        value = inside(terms, keys)
        for name, member in written.items():
            if member == "absent":
                del value[name]
            else:
                value[name] = member
        return value

    monkeypatch.setattr(_lease, "lease_inside", written_inside)
    event = _lease.lease_event(
        registry=registry,
        sale=sale,
        terms=opened.value.terms,
        created_at=case["event"]["created_at"],
        random=secrets.token_bytes,
    )
    assert event is not None
    again = _lease.open_sale_record(
        event.to_json(), sale=sale, registry=registry, registry_address=registry_address
    )
    if opens:
        assert isinstance(again, Opened)
        assert again.value.terms is not None
        assert (again.value.terms.invoice, again.value.terms.verify_url) == (None, None)
    else:
        # Not the sale's lease: no record of it at all.
        assert again is None


@pytest.mark.parametrize("sale", ["absent", None])
def test_a_witness_without_sale_does_not_open_and_still_names_its_address(sale: object) -> None:
    """SPEC.md §31, Witnesses: `sale` is null for `none`; absent is not null (SPEC.md §2): such
    a witness does not open. Its event, of the seen author and checking out, still names the
    address whose tag it holds (Leasing an address, step 3: whatever its content)."""
    import secrets

    from fakes.chain import deposit_address
    from sattleme_checkout._constants import WITNESS_TYPE
    from sattleme_checkout._crypto import Secret
    from sattleme_checkout._derive import address_tag, order_tag, witness_address
    from sattleme_checkout._records import Opened, seal
    from sattleme_checkout._witness import addresses_named, open_witness

    seen = _seen()
    channel_key = Secret(bytes.fromhex(WITNESSES["channelEncryptionKey"]))
    address, txid = deposit_address(), "ef" * 32
    value: dict[str, Any] = {
        "version": 1,
        "address": address,
        "txid": txid,
        "sale": None,
        "sats": 1000,
        "inputs": [f"{'12' * 32}:0"],
        "replacement": False,
        "expiresAt": None,
    }
    if sale == "absent":
        del value["sale"]
    event = seal(
        record_type=WITNESS_TYPE,
        value=value,
        signer=seen.secret,
        key=channel_key,
        address=witness_address(seen, address, txid, None),
        created_at=1_791_378_000,
        random=secrets.token_bytes,
        tags=[["o", order_tag(seen, address, None)], ["o", address_tag(seen, address)]],
    )
    opened = open_witness(event.to_json(), seen, channel_key)
    if sale == "absent":
        assert opened is None
    else:
        assert isinstance(opened, Opened)
        assert opened.value.sale is None
    by_tag = {address_tag(seen, address): address}
    assert addresses_named(event.to_json(), seen, by_tag) == frozenset({address})
