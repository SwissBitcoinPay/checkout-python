"""Sealed records (SPEC.md §12): events of kind 30078 whose content is NIP-44 v2.

Sealing: `{"t": <type>, "v": <value>}`, then spaces up to the next multiple of 256 bytes,
encrypted under the record's conversation key with a fresh nonce, signed by its author.

Opening, the cheap checks first: kind, author, `d` tag; the content decrypts; it is the type's
JSON; the event checks out (id and signature); then the value is validated (per record). A record
of a newer format (another NIP-44 version, or a value of a newer `version`) is only ever
`NEWER`, and only once its signature verified: an unsigned copy proves nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from . import _json
from ._constants import RECORD_KIND, RECORD_PADDING
from ._crypto import Secret
from ._nip44 import Nip44Error, Nip44NewerVersionError, decrypt, encrypt
from ._nostr import Event, check_event, sign_event

T = TypeVar("T")

RandomBytes = Callable[[int], bytes]


class NewerFormat(Exception):  # noqa: N818 - a signal, not an error
    """Raised by a value's validation: the value is of a newer format than this package reads."""


@dataclass(frozen=True, slots=True)
class Opened(Generic[T]):
    """A record that opened: its event and its validated value."""

    event: Event
    value: T


@dataclass(frozen=True, slots=True)
class Newer:
    """An authentic record of a newer format: never used, never written over."""

    event: Event


def seal(
    *,
    record_type: str,
    value: object,
    signer: Secret,
    key: Secret,
    address: str,
    created_at: int,
    random: RandomBytes,
    tags: Sequence[Sequence[str]] = (),
) -> Event:
    """The signed event of a record (a fresh nonce, fresh auxiliary randomness)."""
    text = _json.dumps({"t": record_type, "v": value})
    length = len(text.encode("utf-8"))
    padded = text + " " * (-length % RECORD_PADDING)
    content = encrypt(padded, key, random(32))
    return sign_event(
        signer,
        created_at=created_at,
        kind=RECORD_KIND,
        tags=[["d", address], *tags],
        content=content,
        aux=random(32),
    )


def plaintext_fits(record_type: str, value: object) -> bool:
    """Whether a record's padded plaintext fits NIP-44 (65 535 bytes)."""
    length = len(_json.dumps({"t": record_type, "v": value}).encode("utf-8"))
    return length + (-length % RECORD_PADDING) <= 65_535


def first_d_tag(value: object) -> str | None:
    """The value of the first `d` tag of what a relay sent, when there is one (a string)."""
    event = _json.as_object(value)
    tags = event.get("tags") if event is not None else None
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d":
            return tag[1] if isinstance(tag[1], str) else None
    return None


def open_record(
    value: object,
    *,
    record_type: str,
    key: Secret,
    address: str,
    author: str,
    parse: Callable[[object, Event], T | None],
) -> Opened[T] | Newer | None:
    """What a relay sent, opened as a record of `record_type` at `address` by `author`.

    `parse` validates the value: it returns None when it is invalid, and raises NewerFormat when
    it is of a newer format. None for anything that does not open.
    """
    event = _json.as_object(value)
    if event is None:
        return None
    kind = _json.as_integer(event.get("kind"))
    content = event.get("content")
    tags = event.get("tags")
    if kind != RECORD_KIND or event.get("pubkey") != author or not isinstance(content, str):
        return None
    if not isinstance(tags, list) or not any(
        isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d" and tag[1] == address
        for tag in tags
    ):
        return None
    newer = False
    payload: object = None
    try:
        text = decrypt(content, key)
    except Nip44NewerVersionError:
        newer = True
    except Nip44Error:
        return None
    else:
        envelope = _json.as_object(_json.parse_text(text))
        if envelope is None or "v" not in envelope or envelope.get("t") != record_type:
            return None
        payload = envelope["v"]
    checked = check_event(value)
    if checked is None:
        return None
    if newer:
        return Newer(checked)
    try:
        parsed = parse(payload, checked)
    except NewerFormat:
        return Newer(checked)
    return None if parsed is None else Opened(checked, parsed)


@dataclass(frozen=True, slots=True)
class Newest(Generic[T]):
    """The newest valid copy of a record, and the date of the newest copy of a newer format."""

    newest: Opened[T] | None
    newest_newer: int | None


def newest_copy(copies: Iterable[Opened[T] | Newer]) -> Newest[T]:
    """Of the copies of a record a relay list sent, opened (each relay's), the newest valid one:
    the highest `created_at`, then the lowest id (as relays keep it)."""
    newest: Opened[T] | None = None
    newest_newer: int | None = None
    for opened in copies:
        if isinstance(opened, Newer):
            created = opened.event.created_at
            newest_newer = created if newest_newer is None else max(newest_newer, created)
        elif newest is None or opened.event.replaces(newest.event):
            newest = opened
    return Newest(newest, newest_newer)
