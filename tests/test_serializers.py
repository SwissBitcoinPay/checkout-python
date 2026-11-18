"""No serializer writes a key (SPEC.md §27): the client, its options and every object holding the
integration key, a key derived from it or CoinGecko's key give nothing of it to what an
application commonly calls on an object it logs or returns: `repr`, `str` and formatting,
`json.dumps` (as it is, with `default=vars`, `default=str` or an object's `__dict__`), `pickle`,
`copy` and `deepcopy`, `dataclasses.asdict` and `astuple`, `vars`, and `logging`. Each either
refuses (an exception, nothing written) or writes none of the keys."""

from __future__ import annotations

import copy
import dataclasses
import io
import json
import logging
import pickle
import secrets
from collections.abc import Callable
from typing import Any

import pytest

from sattleme_checkout import AsyncClient, Client
from sattleme_checkout._channel import ActiveChannel, parse_channel
from sattleme_checkout._checkout import new_link
from sattleme_checkout._crypto import Secret
from sattleme_checkout._declaration import Registry
from sattleme_checkout._derive import link_keys, rest_keys, rest_reader, sale_keys, seen_keys
from sattleme_checkout._key import parse_key
from sattleme_checkout._nostr import check_event
from spec_vectors import vector

APP: Any = vector("app/app.json")
KEY_TEXT: str = APP["secretKey"]
KEY = parse_key(KEY_TEXT)
COINGECKO_KEY = "CG-a-demo-key-of-this-test"


def _objects() -> dict[str, object]:
    sale = sale_keys(KEY.secret, "ORD-1")
    rest = rest_keys(KEY.secret, "ORD-1", "bc1p" + "q" * 58, 1_791_378_900)
    link = new_link(secrets.token_bytes)
    channel = parse_channel(APP["channel"]["value"]["v"], check_event(APP["channel"]["event"]))  # type: ignore[arg-type]
    assert isinstance(channel, ActiveChannel)
    client = AsyncClient(KEY_TEXT, coingecko_api_key=COINGECKO_KEY)
    settings = client._engine.settings
    assert settings.coingecko_api_key is not None
    return {
        "AsyncClient": client,
        "Client": Client(KEY_TEXT, coingecko_api_key=COINGECKO_KEY),
        "the client's engine": client._engine,
        "the client's options": settings,
        "IntegrationKey": KEY,
        "the integration key": KEY.secret,
        "CoinGecko's key": settings.coingecko_api_key,
        "SaleKeys": sale,
        "SeenKeys": seen_keys(KEY.secret),
        "RestKeys": rest,
        "RestReader": rest_reader(rest.key, rest.author),
        "Link": link,
        "LinkKeys": link_keys(link.link_key),
        "ActiveChannel": channel,
        "Registry": Registry(key=channel.registry_key, reader=channel.registry_reader),
    }


def _keys(objects: dict[str, object]) -> list[bytes]:
    """Every key the objects hold, as what is written could hold it: its bytes, their hex, and
    the key's own text."""
    texts = [KEY_TEXT.split(".")[1], COINGECKO_KEY]
    secrets_held = [KEY.secret, sale_keys(KEY.secret, "ORD-1").key]
    for value in objects.values():
        for name in getattr(type(value), "__slots__", ()):
            held = getattr(value, name, None)
            if isinstance(held, Secret):
                secrets_held.append(held)
    assert len(secrets_held) > 10
    found = [text.encode() for text in texts]
    for held in secrets_held:
        found += [held.reveal(), held.reveal().hex().encode()]
    return found


def _logged(value: object) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("test.serializers")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info(value)
        logger.info("%s %r", value, value)
        logger.warning("held: %(value)s", {"value": value})
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


OBJECTS = _objects()
KEYS = _keys(OBJECTS)

SERIALIZERS: dict[str, Callable[[object], object]] = {
    "repr": repr,
    "str": str,
    "format": lambda value: f"{value} {value!r} {value!s}",
    "json.dumps": json.dumps,
    "json.dumps, default=vars": lambda value: json.dumps(value, default=vars),
    "json.dumps, default=str": lambda value: json.dumps(value, default=str),
    "json.dumps, __dict__": lambda value: json.dumps(value, default=lambda o: o.__dict__),
    "pickle": pickle.dumps,
    "copy": lambda value: repr(copy.copy(value)),
    "deepcopy": lambda value: repr(copy.deepcopy(value)),
    "dataclasses.asdict": lambda value: repr(dataclasses.asdict(value)),  # type: ignore[call-overload]
    "dataclasses.astuple": lambda value: repr(dataclasses.astuple(value)),  # type: ignore[call-overload]
    "vars": lambda value: repr(vars(value)),
    "logging": _logged,
}


@pytest.mark.parametrize("serializer", SERIALIZERS)
@pytest.mark.parametrize("held", OBJECTS)
def test_no_serializer_writes_a_key(held: str, serializer: str) -> None:
    value = OBJECTS[held]
    try:
        written = SERIALIZERS[serializer](value)
    except (TypeError, AttributeError, ValueError):
        # Refused: nothing written.
        return
    data = written if isinstance(written, bytes) else str(written).encode("utf-8", "surrogatepass")
    assert [key for key in KEYS if key in data] == []


@pytest.mark.parametrize("held", ["AsyncClient", "Client", "IntegrationKey", "the integration key"])
def test_the_key_is_never_pickled_nor_copied(held: str) -> None:
    """What holds the integration key itself refuses to be pickled or copied: no second object
    holds it, and none is written."""
    value = OBJECTS[held]
    for serialize in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            serialize(value)
