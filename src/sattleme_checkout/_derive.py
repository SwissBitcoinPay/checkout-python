"""Key derivations (SPEC.md §10, §30 Keys, §31 The lot, Witnesses, Rest records, The merchant's
acceptance).

These never change: the merchant's app and its web app derive the same keys. Every secret is a
`Secret`; public values (addresses, authors, tags) are lowercase hex.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._constants import CHECKOUT_SALT, DECLARATION_SALT, INTEGRATION_SALT, SALE_SALT
from ._crypto import Secret, ecdh_x, hkdf, hkdf_extract, hmac_sha256, xonly_public_key


def _integration(key: Secret, info: str) -> Secret:
    return hkdf(key, INTEGRATION_SALT, info)


def channel_address(key: Secret) -> str:
    return _integration(key, "channel/address/v1").reveal().hex()


def channel_encryption_key(key: Secret) -> Secret:
    return _integration(key, "channel/encryption-key/v1")


def taker_key(key: Secret) -> Secret:
    return _integration(key, "taker/v1")


def lot_address(key: Secret) -> str:
    return _integration(key, "onchain-lot/address/v1").reveal().hex()


@dataclass(frozen=True, slots=True)
class SaleKeys:
    """A sale's keys (the order `ref`): what its declarations and leases are made with."""

    ref: str
    key: Secret
    secret: Secret
    author: str
    taker: str


def sale_keys(key: Secret, ref: str) -> SaleKeys:
    sale_key = _integration(key, f"sale/{ref}/v1")
    secret = hkdf(sale_key, SALE_SALT, "nostr-secret-key/v1")
    author = xonly_public_key(secret).hex()
    taker = hmac_sha256(taker_key(key), bytes.fromhex(author))[:16].hex()
    return SaleKeys(ref=ref, key=sale_key, secret=secret, author=author, taker=taker)


def registry_address(registry_key: Secret, payment_hash: str) -> str:
    """The registry address of a payment hash: where its declaration is."""
    return hmac_sha256(registry_key, bytes.fromhex(payment_hash)).hex()


def onchain_registry_address(registry_key: Secret, address: str) -> str:
    """The registry address of a deposit address: where its leases are."""
    return hmac_sha256(registry_key, ("onchain/v1/" + address).encode("utf-8")).hex()


def outer_key(registry_key: Secret, address: str, author: str) -> Secret:
    """The outer key of a registry record at `address` by `author`."""
    return hkdf(registry_key, DECLARATION_SALT, f"outer/v1/{address}/{author}")


def inner_key(sale_secret: Secret, registry_reader: str) -> Secret:
    """NIP-44 v2 conversation key of the sale secret and the registry reader (ECDH)."""
    return hkdf_extract(b"nip44-v2", ecdh_x(sale_secret, bytes.fromhex(registry_reader)))


@dataclass(frozen=True, slots=True)
class SeenKeys:
    """The keys of the integration's witnesses."""

    secret: Secret
    author: str
    address_key: Secret


def seen_keys(key: Secret) -> SeenKeys:
    secret = _integration(key, "onchain-seen/nostr-secret-key/v1")
    return SeenKeys(
        secret=secret,
        author=xonly_public_key(secret).hex(),
        address_key=_integration(key, "onchain-seen/address-key/v1"),
    )


def witness_address(seen: SeenKeys, address: str, txid: str, sale: str | None) -> str:
    text = f"{address}/{txid}/{'none' if sale is None else sale}"
    return hmac_sha256(seen.address_key, text.encode("utf-8")).hex()


def order_tag(seen: SeenKeys, address: str, sale: str | None) -> str:
    text = f"order/{address}/{'none' if sale is None else sale}"
    return hmac_sha256(seen.address_key, text.encode("utf-8")).hex()


def address_tag(seen: SeenKeys, address: str) -> str:
    """A witness's second `o` tag: it finds every witness of the address, whatever its sale."""
    return hmac_sha256(seen.address_key, f"address/{address}".encode()).hex()


def merchant_acceptance_address(key: Secret, sale: str, address: str, expires_at: int) -> str:
    """The address (`d` tag) of the merchant's acceptance of the invoice of the sale `sale` (its
    author) with the deposit address `address` ending at `expires_at`: one per invoice."""
    return _integration(key, f"onchain-accepted/{sale}/{address}/{expires_at}/v1").reveal().hex()


@dataclass(frozen=True, slots=True)
class LinkKeys:
    """A checkout link's keys, from its link key."""

    address: str
    encryption_key: Secret


def link_keys(link_key: Secret) -> LinkKeys:
    return LinkKeys(
        address=hkdf(link_key, CHECKOUT_SALT, "address/v1").reveal().hex(),
        encryption_key=hkdf(link_key, CHECKOUT_SALT, "encryption-key/v1"),
    )


@dataclass(frozen=True, slots=True)
class RestReader:
    """What reads an invoice's rest records and acceptance records: its rest key and author."""

    key: Secret
    author: str
    encryption_key: Secret
    acceptance_address: str

    def record_address(self, payment_hash: str) -> str:
        return hkdf(self.key, CHECKOUT_SALT, f"rest/address/{payment_hash}/v1").reveal().hex()


@dataclass(frozen=True, slots=True)
class RestKeys(RestReader):
    """The rest keys of an invoice with an address; only the integration key derives the secret."""

    secret: Secret


def rest_reader(rest_key: Secret, author: str) -> RestReader:
    """The page's reader of a link's rest records (its record gives the rest key and author)."""
    return RestReader(
        key=rest_key,
        author=author,
        encryption_key=hkdf(rest_key, CHECKOUT_SALT, "rest/encryption-key/v1"),
        acceptance_address=hkdf(rest_key, CHECKOUT_SALT, "rest/accepted/v1").reveal().hex(),
    )


def rest_keys(key: Secret, ref: str, address: str, expires_at: int) -> RestKeys:
    """The rest keys of the invoice of `ref` with deposit `address` ending at `expires_at`."""
    prefix = f"checkout-rest/{ref}/{address}/{expires_at}"
    rest_key = _integration(key, f"{prefix}/key/v1")
    secret = _integration(key, f"{prefix}/nostr-secret-key/v1")
    reader = rest_reader(rest_key, xonly_public_key(secret).hex())
    return RestKeys(
        key=reader.key,
        author=reader.author,
        encryption_key=reader.encryption_key,
        acceptance_address=reader.acceptance_address,
        secret=secret,
    )
