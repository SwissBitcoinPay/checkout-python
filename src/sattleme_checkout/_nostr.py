"""Nostr events (NIP-01, SPEC.md §11): ids, signatures, and the checks of an event a relay sent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import _json
from ._crypto import Secret, schnorr_sign, schnorr_verify, sha256, xonly_public_key
from ._text import encode_utf8, is_hex32, is_hex64


@dataclass(frozen=True, slots=True)
class Event:
    """An event whose id is its hash and whose signature verifies (`check_event`, or signed
    here); but one from `hashed_event`, whose signature a lease's walk checks where it decides."""

    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: tuple[tuple[str, ...], ...]
    content: str
    sig: str

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "created_at": self.created_at,
            "tags": [list(tag) for tag in self.tags],
            "content": self.content,
            "id": self.id,
            "pubkey": self.pubkey,
            "sig": self.sig,
        }

    def tag(self, name: str) -> str | None:
        """The value of the first tag named `name`."""
        for tag in self.tags:
            if len(tag) >= 2 and tag[0] == name:
                return tag[1]
        return None

    def replaces(self, other: Event) -> bool:
        """Whether relays keep this one rather than `other`: later, then the lowest id."""
        return self.created_at > other.created_at or (
            self.created_at == other.created_at and self.id < other.id
        )


def serialization(
    pubkey: str, created_at: int, kind: int, tags: Sequence[Sequence[str]], content: str
) -> str:
    """The NIP-01 serialization of an event (SPEC.md §11): `[0, pubkey, created_at, kind, tags,
    content]` without whitespace, its strings as `JSON.stringify` writes them (NIP-01's seven
    escapes, any other character below U+0020 as `\\u` and four lowercase hex digits, every
    other character as it is)."""
    return _json.dumps([0, pubkey, created_at, kind, [list(tag) for tag in tags], content])


def event_hash(
    pubkey: str, created_at: int, kind: int, tags: Sequence[Sequence[str]], content: str
) -> bytes | None:
    """SHA-256 of the NIP-01 serialization; None when a text is not well-formed Unicode."""
    data = encode_utf8(serialization(pubkey, created_at, kind, tags, content))
    return None if data is None else sha256(data)


def sign_event(
    secret: Secret,
    *,
    created_at: int,
    kind: int,
    tags: Sequence[Sequence[str]],
    content: str,
    aux: bytes,
) -> Event:
    """A signed event, with 32 bytes of auxiliary randomness for BIP-340."""
    pubkey = xonly_public_key(secret).hex()
    frozen_tags = tuple(tuple(tag) for tag in tags)
    digest = event_hash(pubkey, created_at, kind, frozen_tags, content)
    if digest is None:
        raise ValueError("an event's texts must be well-formed Unicode")
    sig = schnorr_sign(digest, secret, aux).hex()
    return Event(
        id=digest.hex(),
        pubkey=pubkey,
        created_at=created_at,
        kind=kind,
        tags=frozen_tags,
        content=content,
        sig=sig,
    )


def check_event(value: object) -> Event | None:
    """The event a relay sent, when it checks out (SPEC.md §11); None for anything else."""
    event = hashed_event(value)
    if event is None or not signature_checks(event.id, event.pubkey, event.sig):
        return None
    return event


def signature_checks(event_id: str, pubkey: str, sig: str) -> bool:
    """Whether `sig` (128 hex characters) is the BIP-340 signature of `event_id` by `pubkey`."""
    if not (is_hex32(event_id) and is_hex32(pubkey) and is_hex64(sig)):
        return False
    return schnorr_verify(bytes.fromhex(event_id), bytes.fromhex(pubkey), bytes.fromhex(sig))


Members = tuple[str, int, int, tuple[tuple[str, ...], ...], str]
"""What an event's id is the hash of: its pubkey, created_at, kind, tags and content."""


def members_of(event: Mapping[str, Any]) -> Members | None:
    """The members an event's id is the hash of, as the check of an event reads them (SPEC.md
    §11): `pubkey` hex32, `created_at` a safe integer and `kind` an integer whatever their
    spelling, `tags` arrays of strings, `content` a string; None for anything else."""
    pubkey = event.get("pubkey")
    created_at = _json.as_safe_integer(event.get("created_at"))
    kind = _json.as_integer(event.get("kind"))
    content = event.get("content")
    raw_tags = event.get("tags")
    if not is_hex32(pubkey) or created_at is None or kind is None:
        return None
    if not isinstance(content, str) or not isinstance(raw_tags, list):
        return None
    tags: list[tuple[str, ...]] = []
    for tag in raw_tags:
        if not isinstance(tag, list) or not all(isinstance(item, str) for item in tag):
            return None
        tags.append(tuple(tag))
    return pubkey, created_at, kind, tuple(tags), content


def _id_of_members(members: Members) -> str | None:
    digest = event_hash(*members)
    return None if digest is None else digest.hex()


def id_of(event: Mapping[str, Any]) -> str | None:
    """The id an event must have (hex), as the check of an event computes it: the hash of its
    members (`members_of`, `event_hash`); None when they are no event's, or a text of theirs is
    not well-formed Unicode."""
    members = members_of(event)
    return None if members is None else _id_of_members(members)


def hashed_event(value: object) -> Event | None:
    """The event a relay sent, when its shape is an event's and its id is its hash; None for
    anything else. Its signature is NOT checked: only a lease's walk takes such an event (SPEC.md
    §31, Opening a lease), and checks the signature where it decides; anything else takes
    `check_event`."""
    event = _json.as_object(value)
    if event is None:
        return None
    event_id, sig = event.get("id"), event.get("sig")
    if not (is_hex32(event_id) and is_hex64(sig)):
        return None
    members = members_of(event)
    if members is None or _id_of_members(members) != event_id:
        return None
    pubkey, created_at, kind, tags, content = members
    return Event(
        id=event_id,
        pubkey=pubkey,
        created_at=created_at,
        kind=kind,
        tags=tags,
        content=content,
        sig=sig,
    )
