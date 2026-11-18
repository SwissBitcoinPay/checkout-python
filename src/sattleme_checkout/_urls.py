"""The URLs of the specification, each in its one spelling: never repaired.

- relay addresses and relay lists (SPEC.md §9);
- checkout origins and redirect URLs (SPEC.md §30);
- an https URL (a pay URL, a callback, a verify URL: SPEC.md §15), and the host and port it
  names.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ._constants import HOST_MAX, REDIRECT_URL_MAX, RELAY_URL_MAX, RELAYS_MAX, RELAYS_MIN
from ._text import ECMASCRIPT_SPACE, is_well_formed

_LABEL = re.compile("[a-z0-9-]+")
_LAST_LABEL = re.compile("[a-z]{2,}")
_PORT = re.compile("[0-9]{1,5}")
_RELAY = re.compile(
    "wss://([a-z0-9.-]+)(?::([0-9]+))?(/[^?#," + re.escape(ECMASCRIPT_SPACE) + "]*)?",
    re.DOTALL,
)
_ORIGIN = re.compile("https://([a-z0-9.-]+)(?::([0-9]+))?")
_REDIRECT_CHARS = re.compile(r"(?:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]|%[0-9A-Fa-f]{2})+")
_REDIRECT_HTTPS = re.compile("https://([a-z0-9.-]+)(?::([0-9]+))?(?:[/?#].*)?", re.DOTALL)
_APP_LINK = re.compile("([a-z][a-z0-9+.-]{0,31})://.*", re.DOTALL)

# SPEC.md §30, Redirect URLs, rule 2: schemes an app's link may not have.
REFUSED_SCHEMES = frozenset(
    {
        # the browser's own, files and scripts
        "about", "blob", "chrome", "chrome-extension", "content", "data", "file", "filesystem",
        "javascript", "moz-extension", "resource", "safari-extension", "vbscript", "view-source",
        # other browsers
        "brave", "com-apple-mobilesafari-tab", "ddgquicklink", "firefox", "firefox-focus",
        "googlechrome", "googlechrome-x-callback", "googlechromes", "microsoft-edge",
        "microsoft-edge-http", "microsoft-edge-https", "onionbrowser", "onionbrowsers",
        "opera-http", "opera-https", "x-safari-http", "x-safari-https",
        # other transports, unencrypted ones
        "ftp", "http", "ws", "wss",
        # any Android activity
        "android-app", "intent",
        # payments
        "bitcoin", "keyauth", "lightning", "liquidnetwork", "lnurl", "lnurlc", "lnurlp", "lnurlw",
        # calls and messages
        "callto", "facetime", "facetime-audio", "mailto", "sip", "sms", "tel",
        # installing software, running system tools
        "itms-services", "ms-msdt", "search-ms",
        # https is rule 1's alone
        "https",
    }
)  # fmt: skip


def is_host(host: str) -> bool:
    """Labels of a-z 0-9 - separated by `.`, the last of two letters at least and not
    `localhost` (RFC 6761: the machine's own loopback, a name under it included), 253 at most."""
    if len(host) > HOST_MAX:
        return False
    labels = host.split(".")
    if len(labels) < 2 or labels[-1] == "localhost":
        return False
    return all(_LABEL.fullmatch(label) for label in labels[:-1]) and bool(
        _LAST_LABEL.fullmatch(labels[-1])
    )


def is_port(port: str) -> bool:
    """1 to 5 digits, from 1 to 65535."""
    return _PORT.fullmatch(port) is not None and 1 <= int(port) <= 65535


def is_relay_url(url: object) -> bool:
    """Whether `url` is a relay address in its one spelling (SPEC.md §9)."""
    if not isinstance(url, str) or len(url) > RELAY_URL_MAX or not is_well_formed(url):
        return False
    match = _RELAY.fullmatch(url)
    if match is None:
        return False
    host, port, path = match.group(1), match.group(2), match.group(3)
    if not is_host(host):
        return False
    if port is not None and not is_port(port):
        return False
    return path is None or not path.endswith("/")


def relay_list(urls: object) -> tuple[str, ...] | None:
    """`urls` as a relay list (1 to 10 relay addresses, none twice); None when it is not one."""
    if not isinstance(urls, list | tuple) or not RELAYS_MIN <= len(urls) <= RELAYS_MAX:
        return None
    if not all(is_relay_url(url) for url in urls):
        return None
    if len(set(urls)) != len(urls):
        return None
    return tuple(urls)


def is_checkout_origin(origin: object) -> bool:
    """`https://`, a host, an optional port, then nothing (SPEC.md §30)."""
    if not isinstance(origin, str):
        return False
    match = _ORIGIN.fullmatch(origin)
    if match is None or not is_host(match.group(1)):
        return False
    return match.group(2) is None or is_port(match.group(2))


def _https_rule(url: str) -> tuple[str, str | None] | None:
    """Rule 1 of a redirect URL, which an https URL is (SPEC.md §15, §30): its characters, 2048 at
    most; `https://`, a host as in a relay address, an optional port; then nothing, or `/`, `?`
    or `#` followed by anything. Its host and port as written; None for anything else."""
    if not 1 <= len(url) <= REDIRECT_URL_MAX or _REDIRECT_CHARS.fullmatch(url) is None:
        return None
    https = _REDIRECT_HTTPS.fullmatch(url)
    if https is None:
        return None
    host, port = https.group(1), https.group(2)
    if not is_host(host) or (port is not None and not is_port(port)):
        return None
    return host, port


def redirect_url_shown(url: object) -> str | None:
    """What the page shows of a valid redirect URL (SPEC.md §30); None when it is not one."""
    if not isinstance(url, str):
        return None
    if url.startswith("https:"):
        https = _https_rule(url)
        if https is None:
            return None
        host, port = https
        return host if port is None else f"{host}:{port}"
    if not 1 <= len(url) <= REDIRECT_URL_MAX or _REDIRECT_CHARS.fullmatch(url) is None:
        return None
    app = _APP_LINK.fullmatch(url)
    if app is None or app.group(1) in REFUSED_SCHEMES:
        return None
    return f"{app.group(1)}://"


def https_host(url: object) -> tuple[str, int] | None:
    """The host and port an https URL names (SPEC.md §15: rule 1 of a redirect URL, so no upper
    case in the scheme or the host, no user name nor password, no IP address, no `localhost`, no
    whitespace, no `\\`; nothing repaired); None when `url` is not one."""
    https = _https_rule(url) if isinstance(url, str) else None
    if https is None:
        return None
    host, port = https
    return host, 443 if port is None else int(port)


def is_https_url(url: object) -> bool:
    return https_host(url) is not None


def same_host(a: str, b: str) -> bool:
    """Whether two https URLs are on the same host: their hosts and their ports equal."""
    host_a, host_b = https_host(a), https_host(b)
    return host_a is not None and host_a == host_b


def join_relay_hosts(relays: Sequence[str]) -> str:
    """Relay addresses as an integration key or a link writes them: hosts without `wss://`."""
    return ",".join(relay[len("wss://") :] for relay in relays)
