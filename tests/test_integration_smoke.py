"""A first run of the client against the local world."""

from __future__ import annotations

from fakes.world import World


async def test_an_order_in_sats_is_paid_in_lightning() -> None:
    world = await World.start()
    try:
        client = world.client()
        invoice = await client.create_invoice(ref="ORD-1", sats=49_000)
        assert invoice.bolt11 is not None
        assert invoice.onchain_address in world.lot
        status = await client.get_status(invoice)
        assert status.state == "open"
        assert invoice.payment_hash is not None
        world.lightning.pay(invoice.payment_hash)
        status = await client.get_status(invoice.to_json())
        assert status.state == "paid"
        assert status.method == "lightning"
    finally:
        await world.close()
