"""The merchant's Lightning address (SPEC.md §16): LUD-16, LUD-06, LUD-12, LUD-21."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import _json
from ._crypto import sha256
from ._http import NO_JSON, Http
from ._text import ascii_lower, encode_utf8, trim_ascii_whitespace, utf8_length
from ._urls import https_host
from .errors import InvalidInvoiceError, NetworkError

_ADDRESS = re.compile(r"([a-z0-9\-_.+]+)@([a-z0-9-]+(?:\.[a-z0-9-]+)+)")


def pay_url(address: str) -> str | None:
    """The pay URL of a Lightning address (LUD-16), an https URL (so its domain is never an IP
    address, SPEC.md §16); None when it is not one."""
    match = _ADDRESS.fullmatch(ascii_lower(trim_ascii_whitespace(address)))
    if match is None:
        return None
    url = f"https://{match.group(2)}/.well-known/lnurlp/{match.group(1)}"
    return url if https_host(url) is not None else None


@dataclass(frozen=True, slots=True)
class Refusal:
    """An LNURL `{"status": "ERROR"}` answer, with its reason."""

    reason: str


def classify(status: int, body: object) -> Refusal | Mapping[str, Any]:
    """An LNURL answer, by its HTTP status and body; NetworkError when it is no answer."""
    refusal_status = 200 <= status < 300 or (400 <= status < 500 and status not in (408, 429))
    obj = _json.as_object(body) if body is not NO_JSON else None
    if refusal_status and obj is not None:
        answer_status = obj.get("status")
        if isinstance(answer_status, str) and ascii_lower(answer_status) == "error":
            reason = obj.get("reason")
            return Refusal(reason if isinstance(reason, str) else "")
    if not 200 <= status < 300 or obj is None:
        raise NetworkError(f"The Lightning address gave no usable answer (HTTP {status})")
    return obj


@dataclass(frozen=True, slots=True)
class PayRequest:
    callback: str
    min_sendable: int
    max_sendable: int
    metadata: str
    comment_allowed: int
    description_hash: str

    @property
    def min_sats(self) -> int:
        return -(-self.min_sendable // 1000)

    @property
    def max_sats(self) -> int:
        return self.max_sendable // 1000

    def sendable(self, sats: int) -> bool:
        return self.min_sats <= sats <= self.max_sats


async def pay_request(http: Http, address: str) -> PayRequest:
    """GET the pay URL: the pay request; NetworkError or InvalidInvoiceError otherwise."""
    url = pay_url(address)
    if url is None:
        raise InvalidInvoiceError("The merchant's Lightning address is not valid")
    status, body = await http.get_json(url)
    answer = classify(status, body)
    if isinstance(answer, Refusal):
        raise NetworkError("The Lightning address refused the pay request")
    callback = answer.get("callback")
    min_sendable = _json.as_safe_integer(answer.get("minSendable"))
    max_sendable = _json.as_safe_integer(answer.get("maxSendable"))
    metadata = answer.get("metadata")
    if answer.get("tag") != "payRequest" or not isinstance(callback, str):
        raise InvalidInvoiceError("The Lightning address gave no pay request")
    if https_host(callback) is None:
        raise InvalidInvoiceError("The pay request's callback is not an https URL")
    if min_sendable is None or max_sendable is None or not 0 <= min_sendable <= max_sendable:
        raise InvalidInvoiceError("The pay request's sendable amounts are not valid")
    hashed = description_hash(metadata) if isinstance(metadata, str) else None
    if not isinstance(metadata, str) or hashed is None:
        raise InvalidInvoiceError("The pay request's metadata is not a string of text")
    comment_allowed = _json.as_safe_integer(answer.get("commentAllowed"))
    return PayRequest(
        callback=callback,
        min_sendable=min_sendable,
        max_sendable=max_sendable,
        metadata=metadata,
        comment_allowed=comment_allowed
        if comment_allowed is not None and comment_allowed >= 0
        else 0,
        description_hash=hashed,
    )


def description_hash(metadata: str) -> str | None:
    """The description hash of the invoices of a pay request whose metadata is `metadata` (hex
    SHA-256 of its UTF-8, LUD-06; its own JSON never read); None when it holds a lone surrogate,
    which has no UTF-8 to hash (SPEC.md §6, §16)."""
    data = encode_utf8(metadata)
    return None if data is None else sha256(data).hex()


def comment_fits(label: str, comment_allowed: int) -> bool:
    """Whether the label is sent as the LNURL comment: its UTF-8 fits `commentAllowed` bytes."""
    length = utf8_length(label)
    return length is not None and 0 < length <= comment_allowed


def callback_url(callback: str, params: Mapping[str, str]) -> str:
    """`callback` with `params` set (as `URLSearchParams.set`): its own parameters are kept, its
    fragment dropped.

    One pass, whatever the callback repeats: the first pair of a name set is given its value, the
    others of that name removed; a name it lacks is added at the end, in the order of `params`.
    """
    parts = urllib.parse.urlsplit(callback)
    query: list[tuple[str, str]] = []
    placed: set[str] = set()
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if name not in params:
            query.append((name, value))
        elif name not in placed:
            placed.add(name)
            query.append((name, params[name]))
    query.extend((name, value) for name, value in params.items() if name not in placed)
    # Its fragment dropped (SPEC.md §17, step 5): what follows it would never reach the server.
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query), fragment=""))


@dataclass(frozen=True, slots=True)
class InvoiceAnswer:
    pr: str
    verify: str


async def request_invoice(
    http: Http, pay: PayRequest, *, sats: int, expiry: int, comment: str | None
) -> InvoiceAnswer:
    """GET the callback for an invoice of `sats` (LUD-06, LUD-12, LUD-21); not checked here."""
    params = {"amount": str(sats * 1000), "expiry": str(expiry)}
    if comment is not None and comment_fits(comment, pay.comment_allowed):
        params["comment"] = comment
    status, body = await http.get_json(callback_url(pay.callback, params))
    answer = classify(status, body)
    if isinstance(answer, Refusal):
        raise NetworkError("The Lightning address refused the invoice")
    pr, verify = answer.get("pr"), answer.get("verify")
    if not isinstance(pr, str) or not isinstance(verify, str):
        raise InvalidInvoiceError("The Lightning address gave no invoice or no verify URL")
    return InvoiceAnswer(pr=pr, verify=verify)
