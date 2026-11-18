# sattleme-checkout

Take Lightning and on-chain payments for a [sattle.me](https://sattle.me) merchant from an online
shop's server, in Python.

It implements the [sattle.me checkout specification](https://github.com/sattleme/checkout-spec)
**1.0.0** (protocol 1), pinned in [`spec/`](https://github.com/sattleme/checkout-python/tree/main/spec)
([`spec/PIN.json`](https://github.com/sattleme/checkout-python/blob/main/spec/PIN.json)): with the
integration's secret key alone, your server makes the invoice of each order (a Lightning invoice,
a deposit address of the merchant's wallet, or both), declares it in the merchant's registry on
the merchant's Nostr relays, and checks its payment. sattle.me runs no server and no API:
payments go straight to the merchant's wallet. The key can make invoices that pay the merchant;
it can never spend funds.

- [Install](#install)
- [The key is a secret](#the-key-is-a-secret)
- [Quick start](#quick-start)
- [Synchronous or asynchronous](#synchronous-or-asynchronous)
- [Orders](#orders)
- [Prices in a currency](#prices-in-a-currency)
- [Payments and their states](#payments-and-their-states)
- [On-chain payments](#on-chain-payments)
- [Checkout links](#checkout-links)
- [Errors](#errors)
- [Options](#options)
- [What goes where](#what-goes-where)
- [Dependencies](#dependencies)
- [Security](#security)
- [Known limits](#known-limits)
- [The specification](#the-specification)
- [Development](#development)
- [Releases](#releases)

## Install

```shell
python -m pip install sattleme-checkout
```

Python 3.11 to 3.15. Every dependency ships wheels for Linux (x86_64 and aarch64), Windows
(x86_64) and macOS (Apple silicon): nothing is compiled at install time there. Elsewhere (an Intel
Mac, Windows on ARM), `cryptography` publishes no wheel of the locked version and builds from its
sources, which needs a Rust toolchain. The package is typed (`py.typed`).

## The key is a secret

The merchant creates the integration in the sattle.me app and gives you its secret key,
`sattleme_ck_…`. Whoever holds it can make invoices and declare sales for the merchant, and read the
deposit addresses the app gives the integration.

- Keep it in your server's secrets: an environment variable, a vault, your platform's secret
  store. Never in code, a repository, a log, an error report, or anything sent to the browser.
- The package never puts it, or any key derived from it, in a message, a log, an exception or a
  `repr`; a client cannot be pickled; an invalid key or option leaves no frame of the package
  holding the key's text (error trackers record the local variables of each frame).
- If it leaks, ask the merchant to remove the integration in the app ("Remove the integration")
  and to create another one: the old key then makes no invoice (`create_invoice`), finds none
  (`find_invoices`) and reads no merchant (`merchant()`): `RevokedError`. A check of an invoice it
  made (`get_status`, `wait_for_payment`) still finds its Lightning payment: an invoice without
  an address is checked as before, and one with an address is `RevokedError` unless its
  Lightning invoice was paid (SPEC.md §14). The merchant settles by hand the orders still being
  paid on-chain.

**One key, one shop.** The merchant creates an integration for each shop (each system with its
own orders' references), and two shops never share a key: an order's records in the merchant's
registry are derived from the key and the order's reference, so two shops with one key would
share their references, and an order of one would be found paid, or pending, with an invoice of
the other. The servers of one shop share its key.

## Quick start

A synchronous server (Django, Flask, any WSGI server, a task queue):

```python
import os

from sattleme_checkout import Client, Price

client = Client(os.environ["SATTLEME_INTEGRATION_KEY"])

# Make the order's invoice, store it with the order, then show it.
invoice = client.create_invoice(ref="order-1042", fiat=Price("24.50", "CHF"), note="Order 1042")
order.invoice = invoice.to_json()  # the JSON of the specification: store it as it is
order.save()
qr_code(invoice.payment_uri)  # one QR code: Lightning, on-chain, or both

# Later (a page refresh, a scheduled job): check the payment of the invoice the order holds.
status = client.get_status(order.invoice)
if status.state == "paid":
    order.mark_paid(proof=status.preimage or status.txids)
```

An asynchronous server (FastAPI, any ASGI server):

```python
import asyncio
import os

from sattleme_checkout import AsyncClient

client = AsyncClient(os.environ["SATTLEME_INTEGRATION_KEY"])


async def pay(order_ref: str) -> None:
    invoice = await client.create_invoice(ref=order_ref, sats=49_000)
    ...  # store invoice.to_json(), show invoice.payment_uri
    async with asyncio.timeout(15 * 60):
        status = await client.wait_for_payment(invoice, on_status=print)
```

Mark an order paid only on a `paid` status of the invoice that order holds: never because the
customer's browser says so.

**Stored invoices are your shop's.** Check only the invoices your shop stored itself with their
orders, where only it writes (its database), never one that comes from the customer (a form field,
a URL, a cookie): `get_status` checks that a stored invoice is well formed (SPEC.md §22), never
that this integration made it. An invoice someone else wrote, with a Lightning invoice of their own
that they paid, reads as `paid` (SPEC.md §27).

## Synchronous or asynchronous

The protocol talks to every relay at once and goes on as soon as a majority answered, so the
package is written once, on asyncio, and offered two ways, with the same options, methods and
errors:

- `AsyncClient`: `await` its methods in your event loop. Cancel a call as any asyncio task
  (`asyncio.timeout`, `task.cancel()`): it stops its requests and raises the cancellation.
- `Client`: its methods block until they are done. Each call runs in an event loop of its own,
  in the calling thread, and takes a `timeout` in seconds (none by default; past it, the call
  stops its requests and raises `TimeoutError`). It never starts an event loop inside one that
  is already running: called from a thread that runs one, it raises `RuntimeError` rather than
  block that loop. There, use `AsyncClient`, or call `Client` from a worker thread
  (`asyncio.to_thread`).

A client keeps nothing between calls but its options: make one per process and share it between
threads (or tasks). Each call reads the merchant's channel again, opens one connection per relay
and closes them when it is done.

**What you inject is called from several tasks at once.** A call asks the relays and the
services at once, in tasks of its event loop: what you inject (`http`, `websocket`, `logger`,
`now`, `random`) is called from several of them within one call, an `http` transport with several
requests in flight, `websocket` opening a connection to each relay at once. Each is called in the
thread that runs the call's event loop (for `Client`, the calling thread: a `Client` shared
between threads calls them from several threads at once), and must be safe to call so. Once a call
returned, nothing it started runs on, cancelled or not: it closed its connections, the relays of
a publication past its majority dropped with them; but for the two threads Python cannot stop, a
name resolution under way in the operating system ([Known limits](#known-limits)) and a custom
rate source's plain function ([Prices in a currency](#prices-in-a-currency)).

## Orders

```python
invoice = client.create_invoice(
    ref="order-1042",  # 1 to 64 of A-Z a-z 0-9 . _ : -; one order, one ref (below)
    sats=49_000,  # or fiat=Price("24.50", "CHF"): exactly one of the two
    duration_seconds=900,  # 60 to 3600: how long the invoice can be paid
    note="Order 1042",  # what the merchant's app shows (the ref when absent)
    checkout=False,  # True: a checkout link too (below)
    redirect_url=None,  # with checkout=True: where its page sends the customer once paid
)
```

`sats` is an `int` or a string of digits. Every invalid parameter raises `InvalidOrderError`
naming it (`param`), before any request.

**One order, one reference.** A `ref` is never reused for another order by anything that holds
the key (a test copy of the shop, a restored database included), and never taken from what the
customer sends: take your order's own identifier.

**One order, one invoice at a time.** The invoices of an order are read in the merchant's
registry, so the rule holds even when your server lost what it stored:

- while an earlier invoice of the order may still be paid (until its end and 120 s), its payment
  is checked first, so that an invoice already paid is never handed out again (underpaid
  on-chain, its rest invoice is asked then, as `get_status` would); then `create_invoice` raises
  `InvoicePendingError` with that invoice (`error.invoice`, and with `checkout=True` a new link
  to it, `error.checkout_url`, whose page shows the rest): show it again. A check that fails is
  the error (the invoice may have been paid): call again;
- past that time, while an on-chain payment of it is being confirmed, it raises
  `InvoicePendingError` with that invoice and no new link (`checkout_url` is `None`: the link you
  got goes on following the payment);
- once an earlier invoice was paid, it raises `OrderPaidError` with that invoice and its status.

Several invoices of an order may still be paid when an earlier call failed once some of the
relays took its invoice, and a later one, which did not read it, made another one: their
payments are checked, and the order's records read again on every relay, at the same time. One
of them paid: `OrderPaidError`, whatever the relays answer; a check failing: its error; fewer
than a majority of the relays answering: `NetworkError`; else the one the merchant's relays hold
on a majority, the one shown, is handed out.

Within one client, calls for the same `ref` run one after the other. Across processes, lock the
order in your database around `create_invoice`. In a store whose reads may lag behind its writes
(an eventually consistent one, as some hosted platforms give), read the lock and what it protects
with reads that see the latest writes (consistent reads).

**Store the invoice** as `invoice.to_json()`: the JSON of the specification, the same in every
language, which `get_status` and `wait_for_payment` take back as it is (an `Invoice`, a
`DeclaredInvoice` or that JSON). `Invoice.from_json(...)` reads it back.

**The specification's JSON is `to_json()`'s, and no other serializer's.** Every type the client
gives, `Invoice`, `DeclaredInvoice`, `PaymentStatus` (whatever its state), `Rest`, `Fiat`,
`RateInfo` and `Merchant`, writes the JSON of SPEC.md §3 with its `to_json()`: store and send
that. `json.dumps` refuses these objects, and `dataclasses.asdict` (or a framework's encoder of
dataclasses) writes Python's names (`expires_at`), amounts as numbers and members a state does
not have, which no implementation reads back.

`find_invoices(ref)` lists the invoices declared for an order, oldest first, to find an order's
invoice again after a failure. It publishes nothing: to show an order's invoice again, call
`create_invoice`, whose `InvoicePendingError` carries it once its records are on a majority of
the relays again. `merchant()` gives the business's name, the Lightning address's
bounds in sats and the number of deposit addresses the integration's lot holds (never the
merchant's Lightning address).

## Prices in a currency

```python
from sattleme_checkout import Price

invoice = client.create_invoice(ref="order-1043", fiat=Price("24.50", "CHF"))
invoice.sats  # 35502 at 69 012 CHF/BTC: rounded up, the merchant never receives less
invoice.fiat  # Fiat(amount='24.50', currency='CHF', minor=2450, digits=2)
invoice.rate  # RateInfo(source='coingecko', currency='CHF', price='69012', ...)
```

`Price` takes strings, never a float: an ISO 4217 code in upper case and an amount with at most
the currency's decimals (the specification's table, `currencies.json`).

```python
from sattleme_checkout import currency_digits

currency_digits("CHF")  # 2: a price in CHF has 2 decimals at most
currency_digits("JPY")  # 0
currency_digits("chf")  # None: no currency an order is priced in
```

`currency_digits(code)` gives the decimals of the currency whose code is exactly `code` in that
table, and `None` for any other text (a code in lower case, a fund, a metal, a test code): it
needs no key and reads nothing. Ask it whether your shop may offer its customers to pay in its
currency, and in how many decimals its prices go to `Price`.

The price of one bitcoin comes from a **rate source**: `"coingecko"`, `"mempool"` (USD, EUR, GBP,
CAD, CHF, AUD, JPY), `"coinbase"`, `"kraken"` or `"opennode"`, or a function of your own. A list
is asked in its order, the next source only when the previous one gives no usable price.
Without `rate_source`, the client asks `DEFAULT_RATE_SOURCES`:
`coingecko, mempool, coinbase, kraken, opennode`. A price its source dates must be less than an
hour old.

```python
from sattleme_checkout import Client, RatePrice, UnsupportedCurrencyError


def my_rates(currency: str) -> RatePrice:  # or an async function
    if currency != "CHF":
        raise UnsupportedCurrencyError("no price", currency=currency)
    return RatePrice(price="69012.50", time=my_feed.updated_at)  # or just the decimal string


client = Client(key, rate_source=[my_rates, "kraken"], coingecko_api_key=os.environ.get("CG_KEY"))
invoice = client.create_invoice(ref="order-1044", fiat=Price("10", "CHF"), rate_source="mempool")
```

A custom source returns a decimal string or a `RatePrice` (never a float); a plain function runs
in a worker thread, so it may block. A call that ends before it returned (cancelled, past its
`timeout`, or ended by another error, such as `InvoicePendingError`) does not wait for it: it runs
on in its thread until it returns, what it returns dropped; an `async` one is cancelled with the
call. An error of this package it raises goes through as it is; any other becomes `RateError`
(its `__cause__` kept). `coingecko_api_key` (CoinGecko's Demo key) is sent to CoinGecko only.

## Payments and their states

`get_status(invoice)` checks the payment now; `wait_for_payment(invoice, interval_ms=2000,
on_status=...)` checks it until it is `paid` or `expired`, handing each status to `on_status` (a
function, or an async one), retrying `NetworkError`s.

| `status.state` | Means                                                                                    |
| -------------- | ---------------------------------------------------------------------------------------- |
| `open`         | Not paid yet; it may still be, until `expires_at` (reported up to 120 s later).          |
| `unconfirmed`  | Enough came on-chain in time; fewer than 3 confirmations yet. Not paid yet.              |
| `paid`         | Paid: `method` is `lightning` (`preimage`), `onchain` (`txids`) or `mixed` (both).       |
| `expired`      | Never paid, for good. `received` above 0: an underpayment the merchant received.         |

`status.to_json()` gives the specification's JSON. `received` counts what came in time, in sats.
A preimage (`paid` in Lightning) is the proof of payment: keep it.

## On-chain payments

From 1 000 sats, an order may also be paid on-chain: the merchant's app gives the integration a
**lot** of deposit addresses of its wallet (filled again whenever the merchant opens the app),
and each order **leases** one for the time of its invoice. By amount:

- within the Lightning address's bounds: a Lightning invoice, and from 1 000 sats an address too
  (`invoice.payment_uri` is then one BIP-21 QR code, `bitcoin:…?amount=…&lightning=…`);
- above its bounds: on-chain only (`OnchainUnavailableError` when no address could be leased:
  retryable, once the merchant opened the app);
- below 1 000 sats: Lightning only.

An address is the order's once its lease is confirmed: published, read again on every relay (no
other order's lease of it, the read complete), then marked confirmed. A lease that is not
confirmed (another server leased the address at once, or the read could not be complete) is
withdrawn, and the next address tried; one that was confirmed is never withdrawn, whatever fails
after (the checkout link, the declaration): it holds its address until its end. An address a
witness of the integration names (a transaction it saw paying it) is never leased again.

An on-chain payment is `unconfirmed`, then `paid` at 3 confirmations on the explorer
(mempool.space, or [the merchant's Bitcoin node](#the-merchants-bitcoin-node)), which takes
about 30 minutes: wait while the customer pays, then check again later with `get_status`. A transaction replaced by another (RBF) keeps its
order; one broadcast after the invoice's end (and its 120 s of grace) is late and pays no order.
A transaction counts for one invoice at most: one that the witness of another invoice names
counts for nothing.

**Underpayments.** When less than the amount came on-chain in time (and unless the merchant's
tolerance or acceptance, below, accepted it), the status stays `open` with `status.rest`: what is
left (`rest.sats`), a Lightning invoice of exactly that (`rest.bolt11`, asked of the merchant's
Lightning address and declared by the check itself) and one QR code for it (`rest.payment_uri`,
on-chain too from 1 000 sats). Paid in time, the order is `paid` with `method: "mixed"`. The
merchant may accept an on-chain payment a little short of the amount (its **tolerance**, set in
its app): read from the channel when the invoice is made, kept in the order's lease, never
written where your page or the customer reads it.

**The merchant's acceptance.** The merchant may also accept, by hand in its app, what an order
underpaid on-chain received as paying it in full: one short by more than its tolerance, which the
merchant would rather settle at what came. Nothing to configure, nothing to call: every check
(`get_status`, `wait_for_payment`, and `create_invoice`'s check of an order's earlier invoices)
reads it on every relay of the merchant with the order's witnesses, and counts it only when a
majority of those relays hold it. The order is then `unconfirmed`, then `paid` (`method: "onchain"`,
or `"mixed"` with invoices of the rest), with `received` below the amount, as with the tolerance:
ship it as for any `paid` status. `wait_for_payment` sees it at its next check of the on-chain part
(within 60 s), the `rest` still given until then. Only the merchant's team can make one: neither
your server's key nor a customer can. The page of a checkout link learns of it as of the tolerance,
through an acceptance record. The limit (SPEC.md §3, §27): a relay that holds the acceptance and
fails at a check after the invoice's end may make that check give `expired`, and a later one
`unconfirmed`, then `paid`: an order you cancelled then is settled by hand.

`wait_for_payment` of an invoice with an address listens to the explorer's WebSocket
(mempool.space's, or the merchant's Bitcoin node's) and checks the on-chain part when it says
something changed (and every 60 s at least); `get_status` never opens it. A failure that each check of a wait meets again (an explorer or the Lightning address
that does not answer, a rest invoice that cannot be asked, a witness that cannot be published) is
logged once, and again only once a check went without it.

**Servers of one shop** run implementations of one version of the specification.

### The merchant's Bitcoin node

The merchant may have the blockchain read from a node of its own that serves mempool.space's API
(an Umbrel, a server of its own) rather than from mempool.space. It sets it in its app, which
writes it in the integration's channel: nothing to configure on your server. Every call reads it
with the channel; every read of an address and of the chain's height (when an address is leased,
at each check), and `wait_for_payment`'s WebSocket, then go to the node: `http://192.168.1.10:3006`
is read at `http://192.168.1.10:3006/api` and `ws://192.168.1.10:3006/api/v1/ws` (`wss://` for an
`https://` node). BlockCypher and the `"mempool"` rate source stay BlockCypher's and
mempool.space's.

- **Its URL** follows a rule of its own (SPEC.md §31): `https`, or `http` for a host of a local
  network only (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, a name under `.local`);
  an IPv4 address or a name, never `localhost`, 127.0.0.0/8 nor 0.0.0.0/8; no user name, query nor
  fragment. A node the channel names in any other way, or with an invalid `fallback` or `payers`,
  is none: mempool.space is read, and the channel stays valid.
- **Its `fallback`**, when the node does not answer a read (no answer, an error status, a
  redirect, an answer out of format):
  - `"mempool"`: the same read of mempool.space, `network` when both fail. A node that failed less
    than 60 s ago in the same call (`create_invoice`, `get_status`, one `wait_for_payment`) is
    asked after mempool.space, so that a node that is down does not make each read wait for its
    timeout. The WebSocket goes to mempool.space's when the node's fails.
  - `"none"`: mempool.space is never asked. While the node does not answer, no address is leased
    (`create_invoice` gives a Lightning invoice only, or `NetworkError` for an order paid on-chain
    only) and every check of an order with an address is `NetworkError`: never `expired` nor
    final underpaid for want of an answer. The order waits until the node answers again.

  A check's two reads (the address's transactions and the chain's height) always come from one
  explorer: when the node fails either, both are asked of mempool.space.

- **A node on the merchant's network** (`http://192.168.1.10:3006`, `http://umbrel.local`) is
  reachable only by a server on that network: a shop the merchant hosts itself, or one that
  joined it (by Tailscale, for one). Any other server fails every read of it, and its fallback
  applies: with `"none"`, no on-chain payment at all. The default `http` transport asks such a
  node in clear HTTP, and no other http URL.
- **`payers`**: with `true` (only for a node of the Internet: `https` and a name, not under
  `.local`), the checkout links' records name the node (its `url` and `fallback`, never `payers`),
  and their page reads the chain from it too, with the same fallback; the node then sees the
  customer's IP address and the address the page follows, and whoever holds a link reads the
  node's URL. With `false`, only your server reads the node, and the page reads mempool.space.
- **Security**: a node's URL is the merchant's choice, and the package follows a valid one: GET
  requests at the paths of mempool.space's API under `/api`, and the WebSocket's two messages,
  in clear HTTP too to a host of a local network, and to an IPv4 address. Nothing is sent but
  deposit addresses, no redirect is followed, and every answer is bounded and checked. But if your
  server holds the keys of several merchants (a shared host), one merchant could so have it ask
  machines of your own network: restrict what your server can reach to public addresses (see
  **Outbound requests** in [What goes where](#what-goes-where)); such a merchant's node is then
  unreachable, and its fallback applies. The node is trusted as mempool.space is: what it shows
  decides whether an order is paid. Over `http`, whoever is on the network between your server and
  the node can change its answers: it is as safe as that network.

## Checkout links

```python
invoice = client.create_invoice(
    ref="order-1045", sats=49_000, checkout=True, redirect_url="https://shop.example/orders/1045"
)
redirect(invoice.checkout_url)  # the merchant's web app shows and checks the invoice
```

The customer pays on the merchant's web app, which sends them to `redirect_url` once paid (an
https URL with a lowercase host, or an app's link such as `myshop://orders/1045`).
`CheckoutUnavailableError` when the merchant's app gives no web app yet. Being sent to the
redirect URL proves nothing (anyone can open it): mark the order paid only after `get_status`
says `paid`. Give `checkout_url` to the order's customer only: it shows the invoice, the
business's name and the note.

## Errors

Every error is a `SattlemeError` with a stable `code` and `retryable` (whether the same
call may succeed later, unchanged); a message never holds a key. A cancellation is not one: the
asyncio cancellation, or `TimeoutError` past a `Client` call's `timeout`.

| Exception                  | `code`                 | Retryable    | What to do                                                       |
| -------------------------- | ---------------------- | ------------ | ---------------------------------------------------------------- |
| `InvalidKeyError`          | `invalid_key`          | no           | The key is not an integration key: check the secret you loaded.  |
| `InvalidOptionError`       | `invalid_option`       | no           | `param` names the client's or the call's option.                 |
| `InvalidOrderError`        | `invalid_order`        | no           | `param` names the order's parameter, or `invoice` (stored).      |
| `RevokedError`             | `revoked`              | no           | The merchant removed the integration.                            |
| `ChannelNotFoundError`     | `channel_not_found`    | unless newer | `newer_format`: update this package.                             |
| `NetworkError`             | `network`              | yes          | A relay or a service did not answer usably: try again.           |
| `InvalidInvoiceError`      | `invalid_invoice`      | no           | The Lightning address's answer failed a check (`check`).         |
| `InvoicePendingError`      | `invoice_pending`      | no           | Show `invoice` again (or `checkout_url`).                        |
| `OrderPaidError`           | `order_paid`           | no           | The order was paid: `invoice`, `status`, `preimage`.             |
| `UnsupportedCurrencyError` | `unsupported_currency` | no           | No source has a price in `currency`.                             |
| `RateError`                | `rate`                 | yes          | No usable price (invalid, too old, a custom source failed).      |
| `CheckoutUnavailableError` | `checkout_unavailable` | no           | No checkout link for this merchant yet.                          |
| `OnchainUnavailableError`  | `onchain_unavailable`  | yes          | Only payable on-chain, and no address could be leased for now.   |

`create_invoice` returns an invoice only once it is declared on a majority of the merchant's
relays (and its address leased, its checkout link published): after any error there is nothing to
show. Call it again: the order gets its invoice, or `InvoicePendingError` with the earlier one.

## Options

`Client(key, ...)` and `AsyncClient(key, ...)` take the same keyword options; an invalid one
raises `InvalidOptionError` naming it, before any request.

| Option              | Default                                                    | Rule                                                         |
| ------------------- | ---------------------------------------------------------- | ------------------------------------------------------------ |
| `rate_source`       | `DEFAULT_RATE_SOURCES`                                     | A source, or a list of them (each name once).                |
| `coingecko_api_key` | none                                                       | CoinGecko's Demo key: visible ASCII (U+0021 to U+007E).      |
| `relay_timeout_ms`  | 10 000                                                     | Per relay request, connection included: 100 to 60 000.       |
| `http_timeout_ms`   | 10 000                                                     | Per HTTP request, body included: 100 to 60 000.              |
| `http`              | the standard library's `http.client`                       | An `HttpTransport` (tests, proxies). Never follow redirects. |
| `websocket`         | `websockets`, 1 MiB a message, no redirect, no compression | `async (url) -> connection` (tests, proxies).                |
| `logger`            | `logging.getLogger("sattleme_checkout")`                   | Warnings only, never a secret.                               |
| `now`, `random`     | `time.time`, `os.urandom`                                  | For tests only.                                              |

Neither default transport uses the environment's proxy settings: to go through a proxy, inject
your own `http` and `websocket`.

## What goes where

- The merchant's **Nostr relays** (the key's, or those the merchant's app names): the channel,
  the declarations, leases, witnesses and checkout links, all encrypted (NIP-44) and signed.
- The merchant's **Lightning address** (breez.tips): asked for each invoice and its payment.
- The **rate sources** you use: asked for a price, with your server's IP address.
- **The explorer** (orders with an address): **mempool.space**, or the merchant's Bitcoin node
  when its app names one (and mempool.space too when the node fails and its fallback allows it):
  the address and its transactions; a payment counts as made on what that one explorer shows.
  With the node's `payers` true, the node also sees the customer's IP address and the address the
  checkout link's page follows. **api.blockcypher.com**: a transaction, only for one no check saw
  in time.

Never the key: every one of them sees your server's IP address and what it asks. Every HTTP
request, and the opening handshake of each WebSocket, sends `User-Agent:
sattleme-Checkout/1`, the same in every implementation and version of the specification.

**Outbound requests.** Besides fixed hosts (the rate sources, mempool.space, BlockCypher), your
server asks hosts that others choose: the merchant's Lightning address, relays and Bitcoin node,
and that address's provider (its callback, its verify URLs). No redirect is followed, and their
URLs are checked first: no IP address, no `localhost` nor a name under it, TLS before any
request; but for the node, whose URL allows `http` to a host of a local network and an IPv4
address, never `localhost` ([The merchant's Bitcoin node](#the-merchants-bitcoin-node)). A name
may still lead to an address of your own network (an internal name, a public name resolving to
a private address). If your server holds the keys of several merchants, or sits in a network with
services of its own, restrict what it can reach to public addresses: in your network, or in the
`http` and `websocket` you inject (a merchant's node on a private address is then unreachable,
and its fallback applies). The package does not resolve names itself to refuse private
addresses: a name can resolve again to another address (DNS rebinding).

## Dependencies

The standard library first: SHA-256 and HMAC (`hashlib`, `hmac`), the HTTP client
(`http.client`, which never follows a redirect), JSON, base64, big integers. Three packages for
the rest, each because the standard library has no such thing:

| Package                                                          | Why                                                                                                                         | Rejected alternatives                                                                                                                         | License                     |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| [`btclib-secp256k1`](https://pypi.org/project/btclib-secp256k1/) | secp256k1: BIP-340 Schnorr signatures, x-only keys, ECDH. A binding (cffi) to [libsecp256k1](https://github.com/bitcoin-core/secp256k1) v0.8.0. | `coincurve` (its last release has no wheels for Python 3.14 and 3.15, and its sources did not build on 3.14), `secp256k1` (no wheels after 3.13), pure-Python curves (never the curve written by hand: not constant time). | MIT                         |
| [`cryptography`](https://pypi.org/project/cryptography/)         | HKDF and ChaCha20 (NIP-44 v2), on OpenSSL; maintained by the Python Cryptographic Authority, and already on most Python servers. | `pycryptodome` (a second C cryptography library beside the one most servers carry), a ChaCha20 written by hand.                     | Apache-2.0 OR BSD-3-Clause  |
| [`websockets`](https://pypi.org/project/websockets/)             | The Nostr relays and the explorer's WebSocket, on asyncio, with a message limit.                                            | `aiohttp` (a whole HTTP stack the package does not need), `wsproto` (the I/O to write ourselves).                                            | BSD-3-Clause                |

Their own dependencies: `cffi` (MIT-0) and `pycparser` (BSD-3-Clause).

**What "audited" rests on.** No published audit covers `btclib-secp256k1` itself: what it rests on
is that it binds [libsecp256k1](https://github.com/bitcoin-core/secp256k1), the library of
Bitcoin Core, developed and reviewed in the open for Bitcoin, whose signing and key generation
are constant time (its README). The release this package locks (0.8.0.14) builds libsecp256k1
v0.8.0: its sources in the package are identical to the upstream tag (commit `6e2c8bc`), and
every one of its 70 files on PyPI carries a PEP 740 attestation of its build by the
`btclib-org/btclib-secp256k1` repository's release workflow. The binding's own code (some 8 100
lines of Python around cffi, besides some 3 700 in its secp256k1-zkp modules, which this package
never imports) is that project's, far less used than libsecp256k1 itself: its pin is exact, and an
update is read before it is taken. The package tests it with the official BIP-340 vectors and the official NIP-44
vectors (`tests/vectors/`, `spec/vectors/nip44/`).

**Versions.** `uv.lock` pins every package exactly, with its hashes, and the CI installs it as it
is; no version less than 7 days old is ever locked (`exclude-newer`), and Dependabot waits as
long. What the package declares to pip leaves the integrator's own resolver room where it is
safe: `btclib-secp256k1` exactly (few projects share it, and its fourth version number may break
its interface), `cryptography` from 50.0.2 on (shared by many packages, and its security fixes
must reach the merchant's server without waiting for a release of this one), `websockets` from
17.4 (which refuses a message fragmented into more than 4 096 frames: an earlier one let a relay
exhaust the server's memory with them) and below 18.

## Security

- **Constant time.** What handles a secret on the curve is libsecp256k1's, Bitcoin Core's library,
  through `btclib-secp256k1` (see [Dependencies](#dependencies)): signing (BIP-340,
  `secp256k1_keypair_create` then `secp256k1_schnorrsig_sign32`) and a key's public key run in
  constant time and with constant memory access in the secret, as libsecp256k1's README says of
  signing and key generation ("Constant time, constant memory access signing and public key
  generation"); the ECDH of NIP-44 is `secp256k1_ecdh`, which multiplies by the secret with
  `secp256k1_ecmult_const` ("in constant-time for q", `src/ecmult_const.h`), through a hash
  function in C, compiled into the binding, that copies the shared point out (NIP-44 takes its x
  unhashed). The binding randomizes its context once loaded (`secp256k1_context_randomize`), a
  blinding against side channels. HKDF and ChaCha20 are OpenSSL's (`cryptography`), HMAC-SHA256
  the standard library's, and MACs are compared with `hmac.compare_digest`. The Python code around
  them is not constant time (the shared point and the keys are Python `bytes` between those calls,
  a sale's key is written in hex into its records), and Python cannot erase a key from memory (its
  `bytes` are immutable): the key lives in the server's memory as long as the client does. Keep it
  in a server you trust.
- **The merchant's Bitcoin node.** A merchant may have your server read the chain from a node of
  its own, `http` to a host of a local network and an IPv4 address included: a server that holds
  the keys of several merchants restricts what it can reach to public addresses (see
  [The merchant's Bitcoin node](#the-merchants-bitcoin-node), Security).
- **Reports**: [SECURITY.md](https://github.com/sattleme/checkout-python/blob/main/SECURITY.md).

## Known limits

- **Platforms.** The CI runs every supported Python version on Linux (x86_64) and Windows
  (x86_64). macOS, ARM machines, PyPy and the free-threaded builds of Python are not tested by the
  CI (see [Install](#install) for the wheels).
- **Waiting.** An on-chain payment takes about 30 minutes to be `paid`: a server that cannot wait
  that long checks again later with `get_status`. `wait_for_payment` keeps one WebSocket open and
  opens new relay connections at each check.
- **Timeouts.** Each relay request and each HTTP request is bounded by its timeout. A request
  that times out or is cancelled shuts its socket at once, wherever it is (connecting, TLS,
  reading); a name resolution under way in the operating system cannot be interrupted: its thread
  ends when the resolution returns, and makes no connection. A `create_invoice` cancelled (or past
  a `Client` call's `timeout`) once it published a lease, before that lease was confirmed,
  publishes nothing more: the lease is not withdrawn, never gives its invoice an address, and
  holds the address until its end (4 320 s at most).
- **Reads on every relay.** Three reads decide that a record is nobody else's: a lease's
  confirmation, the witnesses read again after a check published one, and the choice among
  several invoices of an order. They wait for every relay of the merchant's list, one
  `relay_timeout_ms` after a majority answered at most, so that a relay that hides a record
  cannot hide it while another one that holds it answers in time; a relay that ran out of time,
  or lost its connection, earlier in the call is not waited for again. Every other read goes on
  at a majority.
- **Leases anybody can write.** Anybody may write events where a lease is (its place is public):
  the leases of the lot are read page after page of 100 events, newest first, and a read they
  fill (one second holding more than a page, 20 full pages) is not complete; the order then goes
  without that address, never with one another order holds. It relies on relays that answer a
  filter's `limit` with their newest events, as many as the limit when they hold as many (strfry,
  nostr-rs-relay): the merchant's relays are such relays.
- **What third parties send.** Every answer is checked before it is used, within the bounds the
  specification sets (1 MiB a message or an HTTP body, 10 MiB an answer's head, its status line,
  headers, a chunked body's size lines and trailers in all, the answer to a WebSocket's
  handshake included, and each line of it 64 KiB at most, 8 KiB of a handshake's; 1000 events
  a query, 20 pages). A read keeps of each event only what it needs, as
  the event comes (its id and date, and the record it opens to, of its event the NIP-01 members
  alone; of a lease, which anybody can copy under another id, a few bytes), and reads the first
  event of each id a relay sends, a later one (a copy, or a forgery) only counted: what a relay
  sends never piles up, whatever its size, and an answer is never cut at a size (events anybody
  wrote where a lease is end a read of leases incomplete, never failed).
  The relays' WebSocket offers no compression, whose few bytes could unpack into many messages
  held at once. Parsing what is received runs on the event loop: a BOLT11 invoice of 1 MiB takes some
  0.6 s of CPU, an explorer's answer of 1 MiB made for it about 1 s. mempool.space lists 75
  transactions of an address at most (50 unconfirmed, 25 confirmed), and so does a merchant's node
  that serves its API; a check publishes a witness, and may ask BlockCypher, for each one it has
  not seen yet, one after the other.

## The specification

[`spec/`](https://github.com/sattleme/checkout-python/tree/main/spec) holds the files of
the specification, unchanged, at commit `1bd7966d0cff77c2538c00e8929b65f0796d9220` (version 1.0.0)
with their SHA-256 in
[`spec/PIN.json`](https://github.com/sattleme/checkout-python/blob/main/spec/PIN.json);
`tests/test_spec_pin.py` fails on any difference, and every
vector file is read by a test. To follow a new version:

```shell
python scripts/pin_spec.py /path/to/a/checkout-spec/clone <commit>
```

then update the code until the new vectors pass, in one pull request. A disagreement between the
specification and this package is reported to
[checkout-spec](https://github.com/sattleme/checkout-spec) (privately, by its
SECURITY.md, for anything that could cost a merchant money or leak a key).

## Development

With [uv](https://docs.astral.sh/uv/) 0.13.7 (`.github/ci-requirements.txt`):

```shell
uv sync --locked
uv run --locked pytest            # never the network: local relays and fakes
uv run --locked ruff format --check . && uv run --locked ruff check .
uv run --locked mypy
```

## Releases

Releases are the maintainers' only: a maintainer pushes a tag `vX.Y.Z` (the version of
`pyproject.toml`) on a commit of `main`;
[`release.yml`](https://github.com/sattleme/checkout-python/blob/main/.github/workflows/release.yml) runs the
CI again, checks the tag, builds the package from the lockfile, and publishes it to PyPI by
trusted publishing, in the `pypi` environment, once a maintainer approves.

## License

MIT, see [LICENSE](https://github.com/sattleme/checkout-python/blob/main/LICENSE).
