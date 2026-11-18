"""The integration key (SPEC.md §8): `sattleme_ck_<team>.<key>.<relays>`."""

from __future__ import annotations

from typing import NoReturn

from ._constants import KEY_PREFIX
from ._crypto import Secret, is_xonly_point
from ._text import base64url_decode, decode_utf8
from ._urls import relay_list
from .errors import InvalidKeyError


class IntegrationKey:
    """A parsed integration key. A secret: its representations never show it."""

    __slots__ = ("__relays", "__secret", "__team")

    def __init__(self, team: str, secret: Secret, relays: tuple[str, ...]) -> None:
        self.__team = team
        self.__secret = secret
        self.__relays = relays

    @property
    def team(self) -> str:
        """The team's public key (hex, x-only): the channel's author."""
        return self.__team

    @property
    def secret(self) -> Secret:
        """The integration's key (32 bytes)."""
        return self.__secret

    @property
    def relays(self) -> tuple[str, ...]:
        """The key's relay list."""
        return self.__relays

    def __repr__(self) -> str:
        return "<IntegrationKey>"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("an integration key is never serialized")


def parse_key(text: object) -> IntegrationKey:
    """Parse an integration key, in the order of SPEC.md §8; else InvalidKeyError.

    The message never holds the key, nor any part of it; nor does the traceback: the frame that
    raises no longer holds the text (error trackers capture the local variables of each frame).
    """
    parsed = _parse(text)
    del text
    if isinstance(parsed, str):
        raise InvalidKeyError(parsed)
    return parsed


def _parse(text: object) -> IntegrationKey | str:
    """The key, or why it is not one. Returns, never raises: no traceback holds its frame."""
    if not isinstance(text, str) or not text.startswith(KEY_PREFIX):
        return "Not an integration key: it must start with sattleme_ck_"
    parts = text[len(KEY_PREFIX) :].split(".")
    if len(parts) != 3:
        return "Not an integration key: it must have three parts"
    team, key, relays = (base64url_decode(part) for part in parts)
    if team is None or key is None or relays is None:
        return "Not an integration key: a part is not base64url"
    if len(team) != 32 or len(key) != 32:
        return "Not an integration key: a key is not 32 bytes"
    if not is_xonly_point(team):
        return "Not an integration key: the team's key is not a public key"
    hosts = decode_utf8(relays)
    if not relays or hosts is None:
        return "Not an integration key: its relays are not valid"
    urls = relay_list(["wss://" + host for host in hosts.split(",")])
    if urls is None:
        return "Not an integration key: its relays are not valid"
    return IntegrationKey(team.hex(), Secret(key), urls)
