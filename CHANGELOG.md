# Changelog

Versions of `sattleme-checkout` ([semantic versioning](https://semver.org)). Each names the
version of the [specification](https://github.com/sattleme/checkout-spec) it implements.

## 1.0.0

First version. Follows version 1.0.0 of the specification (protocol 1) and passes its
conformance vectors.

- `Client` (synchronous code) and `AsyncClient` (asyncio), with the same options and methods:
  `merchant`, `create_invoice`, `get_status`, `wait_for_payment` and `find_invoices`. `Client`
  runs each call in an event loop of its own, and leaves the calling thread's current event loop
  as it was. A call asks the relays and services at once; once it returned, cancelled or not,
  nothing it started runs on (its connections closed, a relay's that never answers the closing
  handshake included), but a custom rate source's plain function and a name resolution, threads
  Python cannot stop.
- Orders in sats or in a currency (the five rate sources of the specification, or your own), the
  methods of an order by its amount: a Lightning invoice, a deposit address of the merchant's
  wallet, or both.
- `currency_digits(code)`: the decimals of a currency an order may be priced in (the
  specification's table), `None` for any other text; it needs no key and reads nothing.
- Declarations in the merchant's registry on a majority of its relays, one invoice at a time per
  order, checkout links, on-chain payments (leases, witnesses, replacements, the rest of an
  underpayment, the merchant's tolerance), and `wait_for_payment` woken by the explorer's
  WebSocket.
- One explorer: an on-chain payment is paid at 3 confirmations on the explorer read,
  mempool.space or the merchant's Bitcoin node; BlockCypher tells when a transaction no check saw
  in time was received.
- The merchant's Bitcoin node: the node the integration's channel names (`bitcoinNode`: its URL,
  `http` allowed for a local network, its `fallback` "mempool" or "none", and `payers`), read
  over REST and WebSocket with its fallback, a node that failed less than 60 s ago in the same
  call being asked after mempool.space, a check's two reads always of one explorer; with "none",
  mempool.space is never asked, and a node that does not answer leases no address and gives
  `network`, never `expired`. With `payers` true, the checkout links' records name the node,
  whose page then reads it too. An invalid `bitcoinNode` is none.
- One order, one invoice at a time: an invoice that may still be paid is checked before it is
  handed out, even alone (one already paid is never handed out again), and asks for its rest
  invoice as any payment check, so that an underpaid one is handed out with the rest invoice its
  checkout links' pages show; of several, the one a majority of the relays holds is handed out.
  Within one client, the asks of rest invoices for one invoice run one at a time.
- On-chain payments: complete reads of the leases of deposit addresses; a lease published
  unconfirmed, confirmed by a read on every relay, then marked (one that was not confirmed is
  withdrawn, a confirmed one never is); the addresses a witness names never leased again;
  witnesses bound to an invoice (its `expiresAt`, an address tag), a transaction counting for one
  invoice at most, a transaction paying the addresses of two orders paying each its part, the
  witnesses read again on every relay after a check published one.
- The merchant's acceptance of an order underpaid on-chain at what it received, made by the
  merchant in its app: read by every check on every relay of the merchant, and counted only when a
  majority of them hold it; the order is then paid with `received` below its amount. A relay that
  holds it and fails at a check after the invoice's end may make that check give `expired`, and a
  later one `paid` (SPEC.md §3).
- What third parties send is checked before it is used: relay reads keep of each event only what
  they need, as it comes, never cut at a size, the first event of each id a relay sends (of a
  lease, which anybody can copy under an id of its own, only its id, author, date, signatures and
  end; of a record, nothing until its event checks out, then its NIP-01 members alone; an id
  longer than 64 characters by its SHA-256); the relays' WebSocket offers no compression; https
  URLs as the specification spells them (no IP address, no `localhost`), the callback's fragment
  dropped, one leading byte order mark of a body ignored, an answer's head bounded at 10 MiB (its
  trailers and a WebSocket's handshake included), a transaction's outputs at every bitcoin, BlockCypher's time
  as BlockCypher writes it; http only to a merchant's Bitcoin node on a local network, by its
  URL's own rule; JSON read in time linear in its size; the `User-Agent` of the
  specification on every request; a failure each check of `wait_for_payment` meets again logged
  once, and nothing it started running once it returned.
- The invoices it stores are the JSON of the specification: `Invoice.to_json()`,
  `Invoice.from_json()`; every type the client gives writes it with `to_json()`, and no other
  serializer writes a key.
