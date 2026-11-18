"""The merchant's app's vectors (vectors/app/): its key, derivations, channel, declaration, lot,
merchant's acceptances; and a declaration made by this package, which opens with the app's keys
(PORTING.md, Definition of done, 2)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sattleme_checkout import _bolt11, _json, _nip44
from sattleme_checkout._channel import ActiveChannel, parse_channel
from sattleme_checkout._constants import CHANNEL_TYPE, DECLARATION_TYPE, LOT_TYPE
from sattleme_checkout._crypto import Secret, sha256, xonly_public_key
from sattleme_checkout._declaration import (
    DeclaredPrice,
    Registry,
    declaration_event,
    declaration_value,
    open_declaration,
)
from sattleme_checkout._derive import (
    channel_address,
    channel_encryption_key,
    inner_key,
    lot_address,
    merchant_acceptance_address,
    outer_key,
    registry_address,
    sale_keys,
    taker_key,
)
from sattleme_checkout._key import IntegrationKey, parse_key
from sattleme_checkout._lot import parse_lot
from sattleme_checkout._merchant_acceptance import (
    NewerSetAside,
    merchant_acceptance_keys,
    merchant_acceptance_of,
    open_merchant_acceptance,
)
from sattleme_checkout._nostr import check_event
from sattleme_checkout._records import Newer, Opened, open_record
from spec_vectors import vector

APP: Any = vector("app/app.json")
IMPLEMENTATION: Any = vector("app/implementation-declaration.json")
LOT: Any = vector("app/onchain-lot.json")
MERCHANT_ACCEPTANCE: Any = vector("app/merchant-acceptance.json")

KEY = parse_key(APP["secretKey"])
CHANNEL = parse_channel(APP["channel"]["value"]["v"], check_event(APP["channel"]["event"]))  # type: ignore[arg-type]
assert isinstance(CHANNEL, ActiveChannel)
REGISTRY = Registry(key=CHANNEL.registry_key, reader=CHANNEL.registry_reader)


def _plaintext(content: str, key_hex: str) -> str:
    return _nip44.decrypt(content, Secret(bytes.fromhex(key_hex)))


def test_the_key_and_its_derivations() -> None:
    assert KEY.team == APP["parsed"]["team"]
    assert KEY.secret.reveal().hex() == APP["parsed"]["integrationKey"]
    assert list(KEY.relays) == APP["parsed"]["relays"]
    derived = APP["derived"]
    assert channel_address(KEY.secret) == derived["channelAddress"]
    assert channel_encryption_key(KEY.secret).reveal().hex() == derived["channelEncryptionKey"]
    assert taker_key(KEY.secret).reveal().hex() == derived["takerKey"]


def test_the_channel_opens() -> None:
    opened = open_record(
        APP["channel"]["event"],
        record_type=CHANNEL_TYPE,
        key=channel_encryption_key(KEY.secret),
        address=channel_address(KEY.secret),
        author=KEY.team,
        parse=parse_channel,
    )
    assert isinstance(opened, Opened)
    channel = opened.value
    assert isinstance(channel, ActiveChannel)
    value = APP["channel"]["value"]["v"]
    assert channel.business_name == value["profile"]["businessName"]
    assert channel.lightning_address == value["profile"]["lightningAddress"]
    assert channel.registry_key.reveal().hex() == value["registryKey"]
    assert channel.registry_reader == value["registryReader"]
    assert channel.relays == tuple(value["relays"])
    assert channel.checkout_origin is None
    assert channel.tolerance == 0
    text = _plaintext(APP["channel"]["event"]["content"], APP["derived"]["channelEncryptionKey"])
    assert json.loads(text) == APP["channel"]["value"]


def test_the_sale_s_keys() -> None:
    declaration = APP["declaration"]
    sale = sale_keys(KEY.secret, declaration["ref"])
    assert sale.key.reveal().hex() == declaration["saleKey"]
    assert sale.secret.reveal().hex() == declaration["saleSecret"]
    assert sale.author == declaration["saleAuthor"]
    assert sale.taker == declaration["taker"]
    address = registry_address(REGISTRY.key, declaration["paymentHash"])
    assert address == declaration["address"]
    assert outer_key(REGISTRY.key, address, sale.author).reveal().hex() == declaration["outerKey"]
    inner = inner_key(sale.secret, REGISTRY.reader)
    assert inner.reveal().hex() == declaration["innerConversationKey"]


def test_the_app_s_declaration_opens() -> None:
    declaration = APP["declaration"]
    sale = sale_keys(KEY.secret, declaration["ref"])
    opened = open_declaration(declaration["event"], sale=sale, registry=REGISTRY)
    assert isinstance(opened, Opened)
    value = opened.value
    assert value.invoice == declaration["invoice"]
    assert value.decoded.payment_hash == declaration["paymentHash"]
    assert value.verify_url == declaration["verifyUrl"]
    assert value.price.to_json() == declaration["fiat"]
    assert value.note == declaration["note"]
    assert value.rest is None
    outside = _plaintext(declaration["event"]["content"], declaration["outerKey"])
    assert len(outside.encode()) == declaration["outsideBytes"]
    # Another sale (another ref) does not open it.
    assert (
        open_declaration(
            declaration["event"], sale=sale_keys(KEY.secret, "ORD-1043"), registry=REGISTRY
        )
        is None
    )


def test_the_real_invoice() -> None:
    real = APP["realInvoice"]
    decoded = _bolt11.decode(real["invoice"])
    assert decoded is not None
    assert str(decoded.amount_msat) == real["amountMsat"]
    assert decoded.timestamp == real["timestamp"]
    assert decoded.expiry == real["expirySeconds"]
    assert decoded.payment_hash == real["paymentHash"]
    assert decoded.description_hash == real["descriptionHash"]
    assert sha256(bytes.fromhex(real["preimage"])).hex() == real["paymentHash"]


def _random(seed: int) -> Any:
    counter = [seed]

    def random(size: int) -> bytes:
        counter[0] += 1
        return bytes([counter[0] % 256]) * size

    return random


def test_a_declaration_made_here_opens_with_the_app_s_keys() -> None:
    declaration = APP["declaration"]
    sale = sale_keys(KEY.secret, declaration["ref"])
    fiat = declaration["fiat"]
    value = declaration_value(
        sale=sale,
        invoice=declaration["invoice"],
        verify_url=declaration["verifyUrl"],
        price=DeclaredPrice(minor=fiat["minor"], digits=fiat["digits"], currency=fiat["currency"]),
        note=declaration["note"],
        rest=None,
    )
    event = declaration_event(
        registry=REGISTRY,
        sale=sale,
        payment_hash=declaration["paymentHash"],
        value=value,
        created_at=declaration["event"]["created_at"],
        random=_random(7),
    )
    assert event is not None
    made = check_event(event.to_json())
    assert made is not None
    assert made.kind == 30078
    assert made.pubkey == declaration["saleAuthor"]
    assert [list(tag) for tag in made.tags] == declaration["event"]["tags"]
    assert made.created_at == declaration["event"]["created_at"]
    outside = _plaintext(made.content, declaration["outerKey"])
    assert len(outside.encode()) % 256 == 0
    envelope = json.loads(outside)
    assert envelope["t"] == DECLARATION_TYPE
    inside = _plaintext(envelope["v"]["sealed"], declaration["innerConversationKey"])
    app_outside = json.loads(_plaintext(declaration["event"]["content"], declaration["outerKey"]))
    app_inside = _plaintext(app_outside["v"]["sealed"], declaration["innerConversationKey"])
    assert inside == app_inside
    # And it opens as the app's does.
    reopened = open_declaration(event.to_json(), sale=sale, registry=REGISTRY)
    assert isinstance(reopened, Opened) and reopened.value.invoice == declaration["invoice"]


def test_the_implementation_s_declaration() -> None:
    sale = sale_keys(KEY.secret, IMPLEMENTATION["ref"])
    assert sale.author == IMPLEMENTATION["saleAuthor"]
    assert sale.taker == IMPLEMENTATION["taker"]
    opened = open_declaration(IMPLEMENTATION["event"], sale=sale, registry=REGISTRY)
    assert isinstance(opened, Opened)
    value = opened.value
    assert value.invoice == IMPLEMENTATION["invoice"]
    assert value.decoded.payment_hash == IMPLEMENTATION["paymentHash"]
    assert str((value.decoded.amount_msat or 0) // 1000) == IMPLEMENTATION["sats"]
    assert value.verify_url == IMPLEMENTATION["verifyUrl"]
    assert value.price.to_json() == IMPLEMENTATION["fiat"]
    assert value.note == IMPLEMENTATION["note"]
    # A second sale claiming the payment of app.json's declaration: the same registry address.
    assert IMPLEMENTATION["event"]["tags"][0][1] == APP["declaration"]["address"]


def test_the_lot_opens() -> None:
    key = parse_key(LOT["secretKey"])
    assert key.team == LOT["team"]
    assert lot_address(key.secret) == LOT["lotAddress"]
    assert channel_encryption_key(key.secret).reveal().hex() == LOT["channelEncryptionKey"]
    opened = open_record(
        LOT["lot"]["event"],
        record_type=LOT_TYPE,
        key=channel_encryption_key(key.secret),
        address=lot_address(key.secret),
        author=key.team,
        parse=parse_lot,
    )
    assert isinstance(opened, Opened)
    assert list(opened.value) == LOT["lot"]["value"]["v"]["addresses"]
    assert (
        _json.parse_text(_plaintext(LOT["lot"]["event"]["content"], LOT["channelEncryptionKey"]))
        == LOT["lot"]["value"]
    )


def test_the_merchant_s_acceptances_are_of_the_team_and_the_channel_s_key() -> None:
    """vectors/app/merchant-acceptance.json: the app's records are of the team of its key, sealed
    with its channel's key; `made`'s, of a team of the vector's own for the same integration."""
    key = parse_key(MERCHANT_ACCEPTANCE["secretKey"])
    assert key.team == MERCHANT_ACCEPTANCE["team"]
    channel_key = channel_encryption_key(key.secret).reveal().hex()
    assert channel_key == MERCHANT_ACCEPTANCE["channelEncryptionKey"]
    made = MERCHANT_ACCEPTANCE["made"]
    made_key = parse_key(made["secretKey"])
    assert made_key.team == made["team"] != key.team
    assert made_key.secret.reveal() == key.secret.reveal()
    assert xonly_public_key(Secret(bytes.fromhex(made["teamSecret"]))).hex() == made["team"]
    for invoice in MERCHANT_ACCEPTANCE["invoices"]:
        sale = sale_keys(key.secret, invoice["ref"])
        assert sale.key.reveal().hex() == invoice["saleKey"]
        assert sale.author == invoice["sale"]
        address = merchant_acceptance_address(
            key.secret, invoice["sale"], invoice["onchainAddress"], invoice["expiresAt"]
        )
        assert address == invoice["acceptanceAddress"]


def _read_merchant_acceptance(record: dict[str, Any], key: IntegrationKey) -> None:
    """A record read as a check reads it for its invoice, with the integration key `key`: through
    the reading of one record (`open_merchant_acceptance`), then through the count of a read on
    every relay where each of 3 relays sent it (`merchant_acceptance_of`)."""
    invoice = MERCHANT_ACCEPTANCE["invoices"][record["invoice"]]
    keys = merchant_acceptance_keys(
        key.secret,
        key.team,
        invoice["sale"],
        invoice["onchainAddress"],
        sats=int(invoice["sats"]),
        expires_at=invoice["expiresAt"],
    )
    opened = open_merchant_acceptance(record["event"], keys)
    found: object
    if isinstance(opened, Opened):
        found = {"received": opened.value, "createdAt": opened.event.created_at}
    elif isinstance(opened, Newer):
        found = "newer"
    else:
        # Ignored, or set aside (one of a newer format dated after the lease's end, told apart
        # for its warning).
        found = None
    assert found == record["expected"]
    counted = merchant_acceptance_of([[opened] if opened is not None else []] * 3, 3)
    expected = record["expected"]
    assert counted.received == (expected["received"] if isinstance(expected, dict) else None)
    # A newer format is warned of, whatever its date.
    assert counted.newer == isinstance(opened, Newer | NewerSetAside)


@pytest.mark.parametrize("record", MERCHANT_ACCEPTANCE["records"], ids=lambda r: r["name"])
def test_a_merchant_s_acceptance_the_app_made(record: dict[str, Any]) -> None:
    _read_merchant_acceptance(record, parse_key(MERCHANT_ACCEPTANCE["secretKey"]))


@pytest.mark.parametrize("record", MERCHANT_ACCEPTANCE["made"]["records"], ids=lambda r: r["name"])
def test_a_merchant_s_acceptance_made_by_the_specification(record: dict[str, Any]) -> None:
    _read_merchant_acceptance(record, parse_key(MERCHANT_ACCEPTANCE["made"]["secretKey"]))
