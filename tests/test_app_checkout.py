"""The app's checkout vectors, end to end (PORTING.md, Definition of done, 3): a checkout link
made by `create_invoice` holds exactly the vector's value; and the rest of an underpayment, asked
by `get_status`, is published in the record the web app reads.

`vectors/app/checkout-link.json` and `checkout-rest.json` give the pay request the merchant's
Lightning address answered and the invoices it made: the fake Lightning address answers that pay
request, so that every invoice check runs as it would."""

from __future__ import annotations

import json
from typing import Any

from fakes.chain import tx
from fakes.lightning import FakeInvoice
from fakes.world import World
from sattleme_checkout import Price
from sattleme_checkout._bolt11 import decode
from sattleme_checkout._checkout import Fragment, parse_fragment, read_link_value
from sattleme_checkout._crypto import Secret
from sattleme_checkout._derive import link_keys, rest_reader
from sattleme_checkout._lnurl import description_hash
from sattleme_checkout._nip44 import decrypt
from sattleme_checkout._nostr import check_event
from sattleme_checkout._rest import open_rest_record
from spec_vectors import vector

LINK: Any = vector("app/checkout-link.json")
REST: Any = vector("app/checkout-rest.json")
PAY_REQUEST: Any = LINK["payRequest"]


def test_the_invoices_pay_the_vectors_pay_request() -> None:
    assert REST["payRequest"] == PAY_REQUEST
    metadata_hash = description_hash(PAY_REQUEST["metadata"])
    for invoice in (LINK["invoice"], REST["link"]["value"]["invoice"], REST["rest"]["invoice"]):
        decoded = decode(invoice)
        assert decoded is not None
        assert decoded.description_hash == metadata_hash


async def _world(**options: Any) -> World:
    """A world whose Lightning address answers the vectors' pay request, exactly."""
    address = dict(json.loads(PAY_REQUEST["metadata"]))["text/identifier"]
    name = address.split("@")[0]
    world = await World.start(
        relays=("relay.example",),
        lightning_address=address,
        metadata=PAY_REQUEST["metadata"],
        **options,
    )
    lightning = world.lightning
    lightning.min_sendable = PAY_REQUEST["minSendable"]
    lightning.max_sendable = PAY_REQUEST["maxSendable"]
    lightning.comment_allowed = PAY_REQUEST["commentAllowed"]
    lightning.callback_path = PAY_REQUEST["callback"].rsplit("/", 1)[1]
    status, served = lightning.handle(f"{lightning.base}/.well-known/lnurlp/{name}")
    assert (status, served) == (200, PAY_REQUEST)
    return world


def _fragment(url: str, origin: str) -> Fragment:
    assert url.startswith(f"{origin}/pay#")
    fragment = parse_fragment(url.split("#", 1)[1])
    assert isinstance(fragment, Fragment)
    return fragment


def _record(world: World, address: str) -> dict[str, Any]:
    found = [e for e in world.received() if ["d", address] in e["tags"]]
    assert len(found) == 1
    return found[0]


def _opened(event: dict[str, Any], key: Secret) -> dict[str, Any]:
    plaintext = decrypt(event["content"], key)
    assert len(plaintext.encode()) % 256 == 0
    value: dict[str, Any] = json.loads(plaintext)
    return value


async def test_a_checkout_link_holds_exactly_the_vectors_value() -> None:
    created_at = decode(LINK["invoice"]).timestamp  # type: ignore[union-attr]
    world = await _world(
        now=created_at,
        lot=[],
        checkout_origin=LINK["origin"],
        business_name=LINK["businessName"],
    )
    try:

        def answer(_invoice: FakeInvoice) -> dict[str, Any]:
            return {"pr": LINK["invoice"], "verify": LINK["verifyUrl"]}

        world.lightning.answer = answer
        # The vector's price of one bitcoin: the order's sats, the vector's invoice.
        client = world.client(rate_source=lambda _currency: LINK["price"])
        order = LINK["order"]
        invoice = await client.create_invoice(
            ref=order["ref"],
            fiat=Price(**order["fiat"]),
            note=order["note"],
            duration_seconds=order["durationSeconds"],
            checkout=order["checkout"],
            redirect_url=order["redirectUrl"],
        )
        assert invoice.sats == int(LINK["sats"])
        assert invoice.bolt11 == LINK["invoice"]
        assert invoice.payment_hash == LINK["paymentHash"]
        assert invoice.expires_at == LINK["expiresAt"]
        assert invoice.redirect_url == order["redirectUrl"]
        assert invoice.checkout_url is not None
        fragment = _fragment(invoice.checkout_url, LINK["origin"])
        assert fragment.relays == tuple(LINK["relays"])
        keys = link_keys(Secret(bytes.fromhex(fragment.link_key)))
        event = _record(world, keys.address)
        assert check_event(event) is not None
        assert event["kind"] == 30078
        assert event["pubkey"] == fragment.author
        assert event["tags"] == [
            ["d", keys.address],
            ["expiration", str(LINK["expiresAt"] + 604_800)],
        ]
        opened = _opened(event, keys.encryption_key)
        assert opened == {"t": "checkout", "v": LINK["value"]}
        assert list(opened["v"]) == list(LINK["value"])
        assert read_link_value(opened["v"]) is not None
        # The link before the declaration: the relay took them in that order.
        kinds = [e["id"] for e in world.relays["relay.example"].received]
        declarations = [e["id"] for e in world.received() if e["id"] != event["id"]]
        assert len(declarations) == 1
        assert kinds.index(event["id"]) < kinds.index(declarations[0])
    finally:
        await world.close()


async def test_the_rest_of_an_underpayment_end_to_end() -> None:
    created_at = REST["createdAt"]
    world = await _world(
        now=created_at,
        lot=[REST["onchainAddress"]],
        checkout_origin=REST["origin"],
        integration_key=bytes.fromhex(REST["integrationKey"]),
    )
    try:
        link_value = REST["link"]["value"]
        rest = REST["rest"]

        def answer(invoice: FakeInvoice) -> dict[str, Any]:
            if invoice.msat == int(REST["sats"]) * 1000:
                return {"pr": link_value["invoice"], "verify": link_value["verifyUrl"]}
            assert invoice.msat == int(rest["sats"]) * 1000
            return {"pr": rest["invoice"], "verify": rest["verifyUrl"]}

        world.lightning.answer = answer
        client = world.client()
        order = REST["order"]
        invoice = await client.create_invoice(
            ref=order["ref"],
            sats=order["sats"],
            note=order["note"],
            duration_seconds=order["durationSeconds"],
            checkout=order["checkout"],
            redirect_url=order["redirectUrl"],
        )
        assert invoice.onchain_address == REST["onchainAddress"]
        assert invoice.created_at == created_at
        assert invoice.expires_at == REST["expiresAt"]
        assert invoice.checkout_url is not None
        fragment = _fragment(invoice.checkout_url, REST["origin"])
        keys = link_keys(Secret(bytes.fromhex(fragment.link_key)))
        opened = _opened(_record(world, keys.address), keys.encryption_key)
        assert opened == {"t": "checkout", "v": link_value}
        assert list(opened["v"]) == list(link_value)

        # 20 000 of its 49 000 sats come on-chain; a minute later, a check asks for the rest.
        payment = REST["transaction"]
        world.explorer.add(
            REST["onchainAddress"],
            tx(payment["txid"], {REST["onchainAddress"]: int(payment["sats"])}),
        )
        world.now = decode(rest["invoice"]).timestamp  # type: ignore[union-attr]
        status = await client.get_status(invoice)
        assert status.to_json() == REST["status"]
        asked = world.lightning.asked[-1]
        assert asked["amount"] == str(int(rest["sats"]) * 1000)
        assert asked["expiry"] == str(REST["expiresAt"] - world.now)

        reader = rest_reader(Secret(bytes.fromhex(link_value["restKey"])), link_value["restAuthor"])
        assert reader.author == rest["restAuthor"]
        assert reader.encryption_key.reveal().hex() == rest["encryptionKey"]
        record = _record(world, rest["address"])
        assert check_event(record) is not None
        assert record["pubkey"] == rest["restAuthor"]
        assert record["tags"] == [
            ["d", rest["address"]],
            ["expiration", str(REST["expiresAt"] + 604_800)],
        ]
        assert _opened(record, reader.encryption_key) == {"t": "checkout-rest", "v": rest["value"]}
        shown = open_rest_record(record, reader, None)
        assert shown is not None
    finally:
        await world.close()
