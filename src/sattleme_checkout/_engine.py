"""What the client does (SPEC.md §4): the merchant, an order's invoice, a payment check, an
order's invoices. Each call reads the channel, opens one connection per relay and closes them when
it is done: nothing is kept between calls but the locks of one order, or of one invoice, at a
time."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ._address import payment_uri
from ._channel import ActiveChannel, read_channel
from ._checkout import RestFields, link_event, link_url, link_value, new_link
from ._constants import (
    GRACE_SECONDS,
    PAYMENT_CHECKS_AT_ONCE,
    RECORD_KIND,
    REST_MIN_LEFT,
    SATS_CURRENCY,
    SATS_MAX,
)
from ._crypto import Secret
from ._declaration import DeclaredPrice, Registry, RestOf, declaration_event, declaration_value
from ._derive import (
    RestKeys,
    SaleKeys,
    SeenKeys,
    channel_encryption_key,
    rest_keys,
    sale_keys,
    seen_keys,
    witness_address,
)
from ._explorer import CallExplorer, Explorer, NodeFailures, Transaction, blockcypher_received
from ._http import Http, HttpTransport
from ._invoice_checks import check_invoice
from ._key import IntegrationKey
from ._lease import SIZED_ADDRESS, LeaseTerms, lease_fits
from ._leasing import Leased, lease_address, read_lot
from ._lnurl import InvoiceAnswer, PayRequest, pay_request, request_invoice
from ._locks import KeyedLock
from ._log import Logger
from ._merchant_acceptance import (
    MerchantAcceptance,
    MerchantAcceptanceKeys,
    merchant_acceptance_keys,
    read_merchant_acceptance,
)
from ._methods import Leasing, methods_after_leasing, methods_by_amount
from ._node import BitcoinNode, record_node_for
from ._nostr import Event
from ._onchain import (
    LightningResult,
    OnchainResult,
    RestInvoice,
    RestPart,
    Tolerance,
    acceptance_received,
    combine,
    onchain_part,
    rest_part,
    status_rest,
)
from ._order import Order
from ._payment import lightning_part
from ._prices import OrderPrice, convert, minor_text
from ._rates import Rate, RateSource, check_quote, first_rate, get_quote, source_name
from ._records import Newer, Opened, RandomBytes
from ._relay import RelayPool, WebSocketConnect, majority
from ._rest import (
    RestShown,
    acceptance_event,
    acceptance_known,
    newest_acceptances,
    open_acceptance,
    open_rest_record,
    rest_record_event,
    should_ask,
)
from ._sales import (
    EarlierInvoice,
    Lease,
    SaleRecords,
    held_on_majority,
    may_still_be_paid,
    pending_outcome,
    read_sale,
)
from ._stored import InvoiceToCheck
from ._tasks import both, bounded, cancel, settle
from ._witness import (
    InvoiceWitnesses,
    Witness,
    invoice_filter,
    invoice_tags,
    invoice_witnesses,
    read_witness,
    witness_event,
)
from .errors import (
    CheckoutUnavailableError,
    InvalidInvoiceError,
    InvalidOrderError,
    InvoicePendingError,
    NetworkError,
    OrderPaidError,
    SattlemeError,
)
from .models import DeclaredInvoice, Fiat, Invoice, Merchant, PaymentStatus, RateInfo


@dataclass(frozen=True, slots=True)
class Settings:
    """The client's options, checked."""

    key: IntegrationKey
    rate_sources: tuple[RateSource, ...]
    # CoinGecko's key, a secret: its ASCII, revealed in its request's headers alone.
    coingecko_api_key: Secret | None = field(repr=False)
    transport: HttpTransport
    connect: WebSocketConnect
    relay_timeout_ms: int
    http_timeout_ms: int
    logger: Logger
    now: Callable[[], int]
    random: RandomBytes


@dataclass(frozen=True, slots=True)
class Shop:
    """The merchant as one call read it: its active channel, its relays and its registry; and
    when the merchant's Bitcoin node failed the call's reads (SPEC.md §31, "in the same call"),
    which every read of the explorer within the call shares."""

    channel: ActiveChannel
    relays: tuple[str, ...]
    registry: Registry
    failures: NodeFailures = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Call:
    """What the checks of one call share (SPEC.md §31, "in the same call"), for wait_for_payment:
    when the merchant's Bitcoin node failed, and who hears the node each channel read names (its
    WebSocket follows it)."""

    failures: NodeFailures = field(default_factory=dict)
    on_node: Callable[[BitcoinNode | None], None] | None = None


@dataclass(frozen=True, slots=True)
class Scope:
    """What a payment check runs within: a call's relays and merchant, and the sale's records
    when that call read them already."""

    pool: RelayPool
    shop: Shop
    records: SaleRecords | None = None


@dataclass(frozen=True, slots=True)
class RestCheck:
    """The rest part as a check left it: the invoice's rest invoices, the Lightning part of each
    (in their order), or the error of reading them."""

    invoices: tuple[RestInvoice, ...] = ()
    results: tuple[LightningResult | SattlemeError, ...] = ()
    error: SattlemeError | None = None

    def part(self) -> RestPart:
        if self.error is not None:
            return RestPart(final=False, error=self.error)
        return rest_part(self.invoices, self.results)


Part = LightningResult | SattlemeError


@dataclass(frozen=True, slots=True)
class Parts:
    """The last result of each part of an invoice (None: it has none)."""

    lightning: Part | None
    onchain: OnchainResult | SattlemeError | None
    rests: RestCheck | None


def status_of(stored: InvoiceToCheck, parts: Parts, now: int) -> PaymentStatus:
    """The status the parts make (SPEC.md §22, §31); raises a part's error when it decides."""
    if stored.onchain_address is None:
        lightning = parts.lightning
        if isinstance(lightning, SattlemeError):
            raise lightning
        if lightning is None:
            raise NetworkError("The invoice's payment was not checked")
        if lightning.state == "paid":
            return PaymentStatus.paid_lightning(lightning.preimage or "")
        if lightning.state == "open":
            return PaymentStatus.open(stored.expires_at, 0, None)
        return PaymentStatus.expired(0)
    rests = parts.rests or RestCheck(error=NetworkError("The invoice's rest was not checked"))
    rest = rests.part()
    onchain = parts.onchain
    if onchain is None:
        onchain = NetworkError("The invoice's on-chain payment was not checked")
    status = combine(
        sats=stored.sats,
        expires_at=stored.expires_at,
        now=now,
        lightning=parts.lightning,
        rest=rest,
        onchain=onchain,
    )
    if status.state != "open":
        return status
    return dataclasses.replace(
        status,
        rest=status_rest(
            address=stored.onchain_address,
            sats=stored.sats,
            expires_at=stored.expires_at,
            received=status.received or 0,
            now=now,
            rests=rests.invoices,
            paid=rest.paid_hashes,
        ),
    )


def to_check(invoice: DeclaredInvoice) -> InvoiceToCheck:
    """What a payment check needs of a declared invoice (read from the registry, valid)."""
    return InvoiceToCheck(
        ref=invoice.ref,
        bolt11=invoice.bolt11,
        payment_hash=invoice.payment_hash,
        verify_url=invoice.verify_url,
        onchain_address=invoice.onchain_address,
        sats=invoice.sats,
        expires_at=invoice.expires_at,
    )


@dataclass(frozen=True, slots=True)
class Payable:
    """What the Lightning part of a payment check needs of an invoice."""

    bolt11: str
    payment_hash: str
    verify_url: str
    expires_at: int


def payable_of(stored: InvoiceToCheck) -> Payable | None:
    """The invoice's own Lightning invoice; None for an order paid on-chain only."""
    if stored.bolt11 is None or stored.payment_hash is None or stored.verify_url is None:
        return None
    return Payable(stored.bolt11, stored.payment_hash, stored.verify_url, stored.expires_at)


def payable_of_rest(invoice: RestInvoice) -> Payable:
    return Payable(invoice.bolt11, invoice.payment_hash, invoice.verify_url, invoice.expires_at)


@dataclass(frozen=True, slots=True)
class Held:
    """An invoice with a deposit address, and its order's reference (its sale's)."""

    stored: InvoiceToCheck
    ref: str
    address: str

    @property
    def sats(self) -> int:
        return self.stored.sats

    @property
    def expires_at(self) -> int:
        return self.stored.expires_at

    def leased_by(self, lease: Lease) -> bool:
        """Whether the sale's lease of the address has this invoice's terms."""
        return lease.terms.sats == self.sats and lease.terms.expires_at == self.expires_at

    def tolerance(self, lease: Lease | None) -> Tolerance:
        """The order's tolerance, its lease's; without it, or another lease of the address, none
        (SPEC.md §31, The merchant's tolerance)."""
        return Tolerance(
            lease.terms.tolerance if lease is not None and self.leased_by(lease) else 0
        )

    def rest_keys(self, key: IntegrationKey) -> RestKeys:
        return rest_keys(key.secret, self.ref, self.address, self.expires_at)


def held_of(stored: InvoiceToCheck) -> Held | None:
    """The invoice's address and order; None without an address."""
    if stored.onchain_address is None:
        return None
    if stored.ref is None:
        raise InvalidOrderError(
            "invoice: an invoice with an address names its order (ref)", param="invoice"
        )
    return Held(stored=stored, ref=stored.ref, address=stored.onchain_address)


def _price_of(fiat: Fiat | None, sats: int) -> DeclaredPrice:
    if fiat is None:
        return DeclaredPrice(minor=sats, digits=0, currency=SATS_CURRENCY)
    return DeclaredPrice(minor=fiat.minor, digits=fiat.digits, currency=fiat.currency)


class _ExplorerIO:
    """What the on-chain part of one invoice reads and writes (SPEC.md §31, The on-chain
    payment): the explorer over HTTP, the witnesses and the merchant's acceptance on the
    merchant's relays, BlockCypher."""

    def __init__(
        self, engine: Engine, scope: Scope, http: Http, held: Held, sale: SaleKeys
    ) -> None:
        self._engine = engine
        self._scope = scope
        self._http = http
        self._address = held.address
        self._expires_at = held.expires_at
        self._sale = sale
        self._seen: SeenKeys = seen_keys(engine.settings.key.secret)
        # The merchant's acceptance of this invoice: the team's record, read on every relay.
        self._accepted: MerchantAcceptanceKeys = merchant_acceptance_keys(
            engine.settings.key.secret,
            engine.settings.key.team,
            sale.author,
            held.address,
            sats=held.sats,
            expires_at=held.expires_at,
        )

    async def transactions(self) -> tuple[int, list[Transaction]]:
        async def read(explorer: Explorer) -> tuple[int, list[Transaction]]:
            # One read, of one explorer: a transaction's confirmations compare its block's height
            # with that explorer's height. Either failing, both are asked of the next explorer.
            transactions, height = await both(
                explorer.address_transactions(self._address), explorer.height()
            )
            return height, transactions

        return await self._engine.explorer(self._scope.shop, self._http).read(read)

    async def witnesses(self, *, again: bool = False) -> InvoiceWitnesses:
        channel_key = channel_encryption_key(self._engine.settings.key.secret)
        tags = invoice_tags(self._seen, self._address, self._sale.author)
        answers: dict[str, list[Opened[Witness] | Newer]] = await self._scope.pool.read(
            self._scope.shop.relays,
            invoice_filter(self._seen, self._address, self._sale.author),
            lambda value: read_witness(value, self._seen, channel_key, self._address, tags),
            every_relay=again,
        )
        return invoice_witnesses(
            (witness for witnesses in answers.values() for witness in witnesses),
            self._sale.author,
            self._expires_at,
        )

    async def merchant_acceptance(self) -> MerchantAcceptance:
        return await read_merchant_acceptance(
            self._scope.pool, self._scope.shop.relays, self._accepted
        )

    def witness_address(self, txid: str, sale: str | None) -> str:
        return witness_address(self._seen, self._address, txid, sale)

    async def publish_witness(self, witness: Witness) -> None:
        await self._engine.publish_witness(self._scope, witness)

    async def received(self, txid: str) -> int | None:
        return await blockcypher_received(self._http, txid)


_NO_CHECKOUT = (
    "The merchant's app gives no web app for checkout links: update the app, or create the "
    "invoice without checkout"
)


class Engine:
    """The client's calls, on checked options and checked arguments."""

    def __init__(self, settings: Settings, *, of: Engine | None = None) -> None:
        self.settings = settings
        # One call of an order, one ask of a rest, at a time: the locks of the engine this one
        # is a view of (`logging_to`), when it is one.
        self._orders: KeyedLock = KeyedLock() if of is None else of._orders
        self._rest_asks: KeyedLock = KeyedLock() if of is None else of._rest_asks

    def logging_to(self, logger: Logger) -> Engine:
        """This engine, its warnings to `logger`: the same options, the same locks."""
        return Engine(dataclasses.replace(self.settings, logger=logger), of=self)

    # The call's connections and merchant.

    def now(self) -> int:
        return self.settings.now()

    def http(self) -> Http:
        return Http(self.settings.transport, self.settings.http_timeout_ms)

    def pool(self) -> RelayPool:
        return RelayPool(
            connect=self.settings.connect,
            timeout_ms=self.settings.relay_timeout_ms,
            random=self.settings.random,
            now=self.now,
        )

    async def shop(self, pool: RelayPool, failures: NodeFailures | None = None) -> Shop:
        """The merchant, read on `pool`; the node's failures of the call in `failures` (a new
        call's own by default)."""
        read = await read_channel(pool, self.settings.key, self.settings.logger)
        channel = read.channel
        return Shop(
            channel=channel,
            relays=read.relays,
            registry=Registry(key=channel.registry_key, reader=channel.registry_reader),
            failures={} if failures is None else failures,
        )

    def explorer(self, shop: Shop, http: Http) -> CallExplorer:
        """The explorer of the call that read `shop`: the merchant's Bitcoin node with its
        fallback, else mempool.space (SPEC.md §31)."""
        return CallExplorer(http, shop.channel.bitcoin_node, shop.failures, self.now)

    async def _on_majority(
        self, pool: RelayPool, shop: Shop, event: Event, sale: SaleKeys, what: str
    ) -> None:
        """Publish an event of the sale on a majority of the merchant's relays, else network."""
        try:
            await pool.publish(shop.relays, event, key=sale.secret)
        except NetworkError as error:
            raise NetworkError(
                f"{what} is not on a majority of the merchant's relays ({error.message}): do not "
                "show this invoice, try again"
            ) from error

    # merchant()

    async def merchant(self) -> Merchant:
        async with self.pool() as pool:
            shop = await self.shop(pool)
            pay, lot = await both(
                pay_request(self.http(), shop.channel.lightning_address),
                read_lot(pool, self.settings.key, shop.relays, self.settings.logger),
            )
        return Merchant(
            business_name=shop.channel.business_name,
            min_sats=pay.min_sats,
            max_sats=pay.max_sats,
            onchain_addresses=len(lot),
        )

    # find_invoices()

    async def find_invoices(self, ref: str) -> list[DeclaredInvoice]:
        async with self.pool() as pool:
            shop = await self.shop(pool)
            records = await self._records(pool, shop, sale_keys(self.settings.key.secret, ref))
        return [earlier.invoice for earlier in records.invoices]

    async def _records(self, pool: RelayPool, shop: Shop, sale: SaleKeys) -> SaleRecords:
        return await read_sale(pool, shop.relays, sale, shop.registry, self.settings.logger)

    # create_invoice()

    async def create_invoice(self, order: Order) -> Invoice:
        # One at a time per order in this client: two calls for one `ref` would both pass the
        # check of its earlier invoices.
        async with self._orders.hold(order.ref), self.pool() as pool:
            return await self._create_invoice(pool, order)

    async def _rate(self, price: OrderPrice, sources: tuple[RateSource, ...]) -> Rate:
        http = self.http()

        async def fetch(source: RateSource, currency: str, _at: int) -> Rate:
            quote = await get_quote(
                source, currency, http=http, coingecko_api_key=self.settings.coingecko_api_key
            )
            # Checked once read: its age, and the RateInfo's `fetchedAt`, are of now.
            return check_quote(source_name(source), quote, currency, self.now())

        return await first_rate(
            sources, price.currency, fetch=fetch, now=self.now, logger=self.settings.logger
        )

    async def _create_invoice(self, pool: RelayPool, order: Order) -> Invoice:
        shop = await self.shop(pool)
        if order.checkout and shop.channel.checkout_origin is None:
            raise CheckoutUnavailableError(_NO_CHECKOUT)
        sale = sale_keys(self.settings.key.secret, order.ref)
        http = self.http()
        # Asked while the registry is read: the Lightning address and the rate need nothing of it.
        pay_task = asyncio.ensure_future(pay_request(http, shop.channel.lightning_address))
        rate_task = None
        if order.price is not None:
            sources = order.rate_sources or self.settings.rate_sources
            rate_task = asyncio.ensure_future(self._rate(order.price, sources))
        tasks: list[asyncio.Future[Any]] = [pay_task]
        if rate_task is not None:
            tasks.append(rate_task)
        try:
            # Its error wins over the other two.
            await self._check_earlier(pool, shop, sale, order)
            pay: PayRequest = await pay_task
            rate = None if rate_task is None else await rate_task
        finally:
            await cancel(tasks)

        if order.price is not None and rate is not None:
            sats = convert(order.price.minor, order.price.digits, rate.price)
            if sats > SATS_MAX:
                raise InvalidOrderError(
                    f"fiat: {sats} sats is more than 21 million bitcoins", param="fiat"
                )
            fiat: Fiat | None = Fiat(
                amount=minor_text(order.price.minor, order.price.digits),
                currency=order.price.currency,
                minor=order.price.minor,
                digits=order.price.digits,
            )
            param = "fiat"
        elif order.sats is not None:
            sats, fiat, param = order.sats, None, "sats"
        else:  # pragma: no cover - the order's check gives one of the two
            raise InvalidOrderError("give either sats or fiat", param="sats")
        methods = methods_by_amount(sats, pay.min_sats, pay.max_sats, param=param)
        price = _price_of(fiat, sats)

        answer: InvoiceAnswer | None = None
        payment_hash: str | None = None
        declaration: Event | None = None
        created_at: int
        expires_at: int
        if methods.lightning:
            answer = await request_invoice(
                http, pay, sats=sats, expiry=order.duration, comment=order.label
            )
            decoded = check_invoice(answer, pay, sats=sats, duration=order.duration, now=self.now())
            payment_hash = decoded.payment_hash
            declaration = self._declaration(
                sale, shop, answer, payment_hash, price, order.label, rest=None
            )
            created_at, expires_at = decoded.timestamp, decoded.expires_at
        else:
            # Without an invoice, the lease's date is the Invoice's.
            created_at = self.now()
            expires_at = created_at + order.duration

        # The address before the link and the declaration: when the lease fails, neither is
        # published.
        leased: Leased | None = None
        if methods.onchain:
            invoice, verify_url = (None, None) if answer is None else (answer.pr, answer.verify)

            def terms(address: str) -> LeaseTerms:
                return LeaseTerms(
                    address=address,
                    sats=sats,
                    invoice=invoice,
                    verify_url=verify_url,
                    created_at=created_at,
                    expires_at=expires_at,
                    price=price,
                    note=order.label,
                    # The order's for good: every check judges its payment with it.
                    tolerance=shop.channel.tolerance,
                )

            # The lease holds the invoice too, sealed twice as the declaration: an invoice it
            # cannot hold is refused now, before anything is published (SPEC.md §17, step 6), by
            # its size alone (every deposit address has one length; no random byte drawn).
            if not lease_fits(terms(SIZED_ADDRESS), sale):
                raise InvalidInvoiceError("The invoice is too large for the lease of an address")
            outcome = await self._lease(
                pool,
                shop,
                sale,
                terms,
                created_at=None if answer is not None else created_at,
            )
            leasing: Leasing = "leased" if isinstance(outcome, Leased) else outcome
            if leasing == "explorer_failed" and methods.lightning:
                self.settings.logger.warning(
                    "No deposit address was leased, the explorer did not answer: the invoice is "
                    "paid in Lightning only"
                )
            methods = methods_after_leasing(methods, leasing)
            if isinstance(outcome, Leased):
                leased = outcome
        address = None if leased is None else leased.address

        # On a failure below, the lease stays: it was confirmed, and holds its address until
        # its end (a withdrawal some relays only took would leave them disagreeing on whether an
        # address that may be the order's is held).
        checkout_url: str | None = None
        # The link before the declaration: when the link fails, nothing is declared and the
        # order gets a new invoice at once; when the declaration fails, the link's URL was never
        # given and its keys are forgotten, so nobody can open it.
        if order.checkout:
            checkout_url = await self._publish_link(
                pool,
                shop,
                order.ref,
                bolt11=None if answer is None else answer.pr,
                verify_url=None if answer is None else answer.verify,
                address=address,
                sats=sats,
                created_at=created_at,
                expires_at=expires_at,
                price=None if fiat is None else price,
                note=order.label,
                redirect_url=order.redirect_url,
            )
        if declaration is not None:
            await self._on_majority(pool, shop, declaration, sale, "The invoice's declaration")

        bolt11 = None if answer is None else answer.pr
        return Invoice(
            ref=order.ref,
            bolt11=bolt11,
            payment_hash=payment_hash,
            verify_url=None if answer is None else answer.verify,
            onchain_address=address,
            payment_uri=payment_uri(address, sats, bolt11),
            sats=sats,
            fiat=fiat,
            rate=None
            if rate is None
            else RateInfo(
                source=rate.source,
                currency=rate.currency,
                price=rate.price.plain(),
                fetched_at=rate.fetched_at,
                time=rate.time,
            ),
            note=order.label,
            created_at=created_at,
            expires_at=expires_at,
            checkout_url=checkout_url,
            redirect_url=order.redirect_url if order.checkout else None,
        )

    def _declaration(
        self,
        sale: SaleKeys,
        shop: Shop,
        answer: InvoiceAnswer,
        payment_hash: str,
        price: DeclaredPrice,
        note: str,
        *,
        rest: RestOf | None,
    ) -> Event:
        # The value holds the sale key: never a local of a frame that raises (SPEC.md §27).
        event = declaration_event(
            registry=shop.registry,
            sale=sale,
            payment_hash=payment_hash,
            value=declaration_value(
                sale=sale,
                invoice=answer.pr,
                verify_url=answer.verify,
                price=price,
                note=note,
                rest=rest,
            ),
            created_at=self.now(),
            random=self.settings.random,
        )
        if event is None:
            # A valid invoice too long for a NIP-44 plaintext (65 535 bytes) cannot be declared.
            raise InvalidInvoiceError("The invoice is too large to be declared")
        return event

    async def _lease(
        self,
        pool: RelayPool,
        shop: Shop,
        sale: SaleKeys,
        terms: Callable[[str], LeaseTerms],
        *,
        created_at: int | None,
    ) -> Leased | Leasing:
        async def publish_witness_none(address: str, tx: Transaction) -> None:
            witness = Witness(
                address=address,
                txid=tx.txid,
                sale=None,
                sats=tx.pays,
                inputs=tx.coins,
                replacement=False,
                seen_at=self.now(),
                expires_at=None,
            )
            try:
                await self.publish_witness(Scope(pool, shop), witness)
            except NetworkError as error:
                self.settings.logger.warning(
                    "The witness of a paid deposit address was not published: %s", error.message
                )

        return await lease_address(
            pool,
            key=self.settings.key,
            registry=shop.registry,
            relays=shop.relays,
            sale=sale,
            explorer=self.explorer(shop, self.http()),
            terms=terms,
            now=self.now,
            random=self.settings.random,
            logger=self.settings.logger,
            publish_witness_none=publish_witness_none,
            created_at=created_at,
        )

    async def _publish_link(
        self,
        pool: RelayPool,
        shop: Shop,
        ref: str,
        *,
        bolt11: str | None,
        verify_url: str | None,
        address: str | None,
        sats: int,
        created_at: int,
        expires_at: int,
        price: DeclaredPrice | None,
        note: str,
        redirect_url: str | None,
    ) -> str:
        """Publish a new checkout link on a majority of the merchant's relays: its URL. Its
        author's secret answers the relays' authentication, then is dropped with the link."""
        origin = shop.channel.checkout_origin
        if origin is None:
            raise CheckoutUnavailableError(_NO_CHECKOUT)
        rest: RestFields | None = None
        if address is not None:
            keys = rest_keys(self.settings.key.secret, ref, address, expires_at)
            rest = RestFields(author=keys.author, key=keys.key.reveal().hex())
        value = link_value(
            business_name=shop.channel.business_name,
            invoice=bolt11,
            verify_url=verify_url,
            onchain=None if address is None else (address, sats, created_at, expires_at),
            price=price,
            note=note,
            redirect_url=redirect_url,
            rest=rest,
            # With `payers` true, the page reads the merchant's Bitcoin node too (SPEC.md §30).
            node=record_node_for(shop.channel.bitcoin_node),
        )
        link = new_link(self.settings.random)
        try:
            event = link_event(
                link,
                value,
                expires_at=expires_at,
                created_at=self.now(),
                random=self.settings.random,
            )
        except ValueError as error:
            raise InvalidInvoiceError("The invoice is too large for a checkout link") from error
        try:
            await pool.publish(shop.relays, event, key=link.author_secret)
        except NetworkError as error:
            raise NetworkError(
                f"The checkout link is not on a majority of the merchant's relays "
                f"({error.message}): try again"
            ) from error
        return link_url(origin, link.author, link.link_key, shop.relays)

    async def _check_earlier(
        self, pool: RelayPool, shop: Shop, sale: SaleKeys, order: Order
    ) -> None:
        """One order, one invoice at a time (SPEC.md §21, The check): read in the registry, so
        that it holds even when the shop lost what it stored."""
        records = await self._records(pool, shop, sale)
        scope = Scope(pool, shop, records)

        async def check(invoices: Sequence[EarlierInvoice]) -> list[PaymentStatus | Error]:
            return await bounded(
                invoices,
                PAYMENT_CHECKS_AT_ONCE,
                lambda earlier: self.status(to_check(earlier.invoice), scope),
            )

        pending = may_still_be_paid(records.invoices, self.now())
        if pending:
            # Their payments checked, even of one alone: an invoice already paid is never handed
            # out again. Several: an earlier call failed once a minority of the relays took its
            # last record, and a later one made another invoice and showed it; the sale's records
            # are then read again on every relay at the same time, and the one shown is the one
            # whose last record a majority of the list sent.
            held: list[bool] | SattlemeError = []
            if len(pending) == 1:
                statuses = await check(pending)
            else:
                statuses, again = await both(
                    check(pending),
                    settle(
                        read_sale(
                            pool,
                            shop.relays,
                            sale,
                            shop.registry,
                            self.settings.logger,
                            every_relay=True,
                        )
                    ),
                )
                if isinstance(again, SattlemeError):
                    held = again
                else:
                    held = held_on_majority(again, pending, len(shop.relays))
            outcome = pending_outcome(statuses, held)
            if isinstance(outcome, SattlemeError):
                # None paid, no check failed, and fewer than a majority answered the read again.
                raise outcome
            kind, index = outcome
            status = statuses[index]
            if isinstance(status, SattlemeError):
                # The first error, none paid: that invoice may have been paid.
                raise status
            if kind == "paid":
                raise _order_paid(pending[index].invoice, status)
            chosen = pending[index]
            # Its first publication may have failed ("do not show this invoice"): on a majority
            # before it is handed out. Its lease, with an address, was confirmed: the address is
            # the order's.
            if chosen.declaration is not None:
                await self._on_majority(
                    pool, shop, chosen.declaration, sale, "The pending invoice's declaration"
                )
            if chosen.lease is not None:
                await self._on_majority(
                    pool, shop, chosen.lease, sale, "The pending invoice's lease"
                )
            invoice = chosen.invoice
            checkout_url = None
            if order.checkout:
                # A new link to it, for a shop that lost the one it stored (or never got it).
                checkout_url = await self._publish_link(
                    pool,
                    shop,
                    order.ref,
                    bolt11=invoice.bolt11,
                    verify_url=invoice.verify_url,
                    address=invoice.onchain_address,
                    sats=invoice.sats,
                    created_at=invoice.created_at,
                    expires_at=invoice.expires_at,
                    price=None if invoice.fiat is None else _price_of(invoice.fiat, invoice.sats),
                    note=invoice.note,
                    redirect_url=order.redirect_url,
                )
            raise InvoicePendingError(
                "An earlier invoice of the order may still be paid",
                invoice=invoice,
                checkout_url=checkout_url,
            )
        statuses = await check(records.invoices)
        for earlier, status in zip(records.invoices, statuses, strict=True):
            if isinstance(status, PaymentStatus) and status.state == "paid":
                raise _order_paid(earlier.invoice, status)
        for earlier, status in zip(records.invoices, statuses, strict=True):
            if isinstance(status, PaymentStatus) and status.state in ("unconfirmed", "open"):
                # An on-chain payment being confirmed, or a transaction undecided.
                raise InvoicePendingError(
                    "An earlier invoice of the order is being paid",
                    invoice=earlier.invoice,
                    checkout_url=None,
                )
        for status in statuses:
            if isinstance(status, SattlemeError):
                raise status

    # get_status()

    async def status(self, stored: InvoiceToCheck, scope: Scope | None = None) -> PaymentStatus:
        return status_of(stored, await self.parts(stored, scope), self.now())

    def lightning_check(self, http: Http, invoice: Payable) -> asyncio.Task[Part]:
        """The Lightning part of an invoice (the order's own, or one of its rest), started now."""

        # The request's coroutine is made once the task runs: a task cancelled before it starts
        # leaves none behind, never awaited.
        async def check() -> Part:
            return await settle(
                lightning_part(
                    http,
                    bolt11=invoice.bolt11,
                    payment_hash=invoice.payment_hash,
                    verify_url=invoice.verify_url,
                    expires_at=invoice.expires_at,
                    now=self.now(),
                )
            )

        return asyncio.ensure_future(check())

    async def parts(
        self, stored: InvoiceToCheck, scope: Scope | None = None, call: Call | None = None
    ) -> Parts:
        """Every part of an invoice checked at once (SPEC.md §31, The on-chain payment); an
        invoice of the rest asked when the status calls for one. A part that fails is its error,
        never raised (a cancellation is). Within `scope` when given (the call's relays and
        merchant); else on relays of its own, as one check of `call` (wait_for_payment's)."""
        http = self.http()
        own = payable_of(stored)
        lightning = None if own is None else self.lightning_check(http, own)
        try:
            held = held_of(stored)
            if held is None:
                return Parts(
                    lightning=None if lightning is None else await lightning,
                    onchain=None,
                    rests=None,
                )
            if scope is not None:
                return await self._address_parts(held, scope, http, lightning)
            try:
                async with self.pool() as pool:
                    shop = await self.shop(pool, None if call is None else call.failures)
                    if call is not None and call.on_node is not None:
                        call.on_node(shop.channel.bitcoin_node)
                    return await self._address_parts(held, Scope(pool, shop), http, lightning)
            except SattlemeError as error:
                # The relays or the channel failed: the address's parts cannot be told.
                return Parts(
                    lightning=None if lightning is None else await lightning,
                    onchain=error,
                    rests=RestCheck(error=error),
                )
        finally:
            if lightning is not None:
                await cancel([lightning])

    async def _address_parts(
        self, held: Held, scope: Scope, http: Http, lightning: asyncio.Task[Part] | None
    ) -> Parts:
        """The parts of an invoice with an address, within `scope`: the sale's records (its rest
        invoices, its lease) read while the explorer is asked; then, when the status is open with
        a rest and no invoice of it, one asked; the records of its rest invoices published again
        where too few relays hold them; its acceptance record when the status calls for one."""
        sale = sale_keys(self.settings.key.secret, held.ref)
        pool, shop = scope.pool, scope.shop
        records_task: asyncio.Future[SaleRecords]
        if scope.records is not None:
            records_task = asyncio.get_running_loop().create_future()
            records_task.set_result(scope.records)
        else:
            records_task = asyncio.ensure_future(self._records(pool, shop, sale))

        async def rest_check() -> RestCheck:
            try:
                records = await asyncio.shield(records_task)
            except SattlemeError as error:
                return RestCheck(error=error)
            # This invoice's own: another invoice of the order may have leased the address.
            invoices = records.rest_invoices(held.address, held.expires_at)
            try:
                for invoice in invoices:
                    # Its declaration may be on a minority only: on a majority before it counts.
                    if invoice.payment_hash in records.unsure:
                        await self._on_majority(
                            pool, shop, records.rest_events[invoice.payment_hash], sale, _REST
                        )
            except NetworkError as error:
                return RestCheck(error=error)
            checks = [self.lightning_check(http, payable_of_rest(i)) for i in invoices]
            try:
                results = tuple([await check for check in checks])
            finally:
                await cancel(checks)
            return RestCheck(invoices=tuple(invoices), results=results)

        async def tolerance() -> Tolerance:
            try:
                records = await asyncio.shield(records_task)
            except SattlemeError:
                # Unread: the payment is judged without tolerance.
                return Tolerance(0)
            return held.tolerance(records.lease_of(held.address))

        async def paid_by_rests() -> int:
            # A rest invoice that failed counts as not paid: the on-chain part then needs more.
            return (await asyncio.shield(rest_task)).part().paid

        io = _ExplorerIO(self, scope, http, held, sale)

        def check_onchain(
            paid: int | asyncio.Future[int], tolerated: Tolerance | asyncio.Future[Tolerance]
        ) -> asyncio.Task[OnchainResult | SattlemeError]:
            # As lightning_check: the part's coroutine is made once the task runs.
            async def check() -> OnchainResult | SattlemeError:
                return await settle(
                    onchain_part(
                        io,
                        address=held.address,
                        sats=held.sats,
                        expires_at=held.expires_at,
                        sale=sale.author,
                        clock=self.now,
                        paid_by_rests=paid,
                        tolerance=tolerated,
                        logger=self.settings.logger,
                    )
                )

            return asyncio.ensure_future(check())

        rest_task = asyncio.ensure_future(rest_check())
        paid_task = asyncio.ensure_future(paid_by_rests())
        tolerance_task = asyncio.ensure_future(tolerance())
        onchain_task = check_onchain(paid_task, tolerance_task)
        tasks: list[asyncio.Future[Any]] = [
            records_task,
            rest_task,
            paid_task,
            tolerance_task,
            onchain_task,
        ]
        try:
            own = None if lightning is None else await lightning
            onchain = await onchain_task
            rests = await rest_task
        finally:
            await cancel(tasks)
        # A rest part that failed (the sale's records unread, or a rest declaration not on a
        # majority): no rest is asked nor recorded.
        if rests.error is None:
            records = records_task.result()
            lease = records.lease_of(held.address)
            now = self.now()
            rest = _rest_to_ask(held, Parts(own, onchain, rests), now)
            # The rest records a majority of the relays took in this check.
            taken: set[str] = set()
            if rest is not None and lease is not None and held.leased_by(lease):
                asked = await self._ask_rest(scope, held, sale, lease, rest, rests, http)
                if asked is not None:
                    rest_invoice, result, recorded = asked
                    if recorded:
                        taken.add(rest_invoice.payment_hash)
                    rests = RestCheck(
                        invoices=(*rests.invoices, rest_invoice), results=(*rests.results, result)
                    )
                    # Asked meanwhile by another check and paid already: less is due on-chain.
                    if isinstance(result, LightningResult) and result.state == "paid":
                        again = check_onchain(rests.part().paid, held.tolerance(lease))
                        try:
                            onchain = await again
                        finally:
                            await cancel([again])
            if rests.invoices and now < held.expires_at + GRACE_SECONDS:
                unrecorded = await self._record_rests(scope, held, rests.invoices, taken)
                rests = dataclasses.replace(
                    rests,
                    invoices=tuple(
                        dataclasses.replace(
                            invoice, recorded=invoice.payment_hash not in unrecorded
                        )
                        for invoice in rests.invoices
                    ),
                )
        # Whatever the rest part: the lease's tolerance, or the merchant's acceptance, may accept
        # the status below `sats`.
        parts = Parts(lightning=own, onchain=onchain, rests=rests)
        await self._publish_acceptance(scope, held, parts)
        return parts

    async def publish_witness(self, scope: Scope, witness: Witness) -> None:
        """A witness on a majority of the merchant's relays, else network (SPEC.md §31)."""
        seen = seen_keys(self.settings.key.secret)
        event = witness_event(
            seen, channel_encryption_key(self.settings.key.secret), witness, self.settings.random
        )
        await scope.pool.publish(scope.shop.relays, event, key=seen.secret)

    async def _ask_rest(
        self,
        scope: Scope,
        held: Held,
        sale: SaleKeys,
        lease: Lease,
        rest_sats: int,
        known: RestCheck,
        http: Http,
    ) -> tuple[RestInvoice, Part, bool] | None:
        """Ask the merchant's Lightning address for an invoice of the rest, declare it with the
        lease's price and note on a majority, then publish its rest record (SPEC.md §31, Asking
        for a rest invoice): the invoice, its Lightning part, and whether a majority of the
        relays took its record. One at a time per invoice in this client, each after the rest
        invoices were read again: another check's invoice of the same rest, declared meanwhile,
        is taken instead. None when none was made: a failure is logged, the next check asks
        again."""
        pool, shop = scope.pool, scope.shop
        async with self._rest_asks.hold(f"{held.ref}/{held.address}/{held.expires_at}"):
            try:
                records = await self._records(pool, shop, sale)
                before = {invoice.payment_hash for invoice in known.invoices}
                now = self.now()
                meanwhile = next(
                    (
                        invoice
                        for invoice in records.rest_invoices(held.address, held.expires_at)
                        if invoice.payment_hash not in before
                        and invoice.sats == rest_sats
                        and now < invoice.expires_at
                    ),
                    None,
                )
                if meanwhile is not None:
                    if meanwhile.payment_hash in records.unsure:
                        await self._on_majority(
                            pool, shop, records.rest_events[meanwhile.payment_hash], sale, _REST
                        )
                    # Checked as any rest invoice: it may have been paid already.
                    check = self.lightning_check(http, payable_of_rest(meanwhile))
                    try:
                        return meanwhile, await check, False
                    finally:
                        await cancel([check])
                pay = await pay_request(http, shop.channel.lightning_address)
                ask = should_ask(
                    rest_sats=rest_sats,
                    rest_bolt11=None,
                    expires_at=held.expires_at,
                    now=self.now(),
                    lease_matches=True,
                    sendable=(pay.min_sats, pay.max_sats),
                )
                if ask is None:
                    return None
                answer = await request_invoice(
                    http, pay, sats=ask.sats, expiry=ask.expiry, comment=lease.terms.note
                )
                decoded = check_invoice(
                    answer, pay, sats=ask.sats, duration=ask.expiry, now=self.now()
                )
                event = self._declaration(
                    sale,
                    shop,
                    answer,
                    decoded.payment_hash,
                    lease.terms.price,
                    lease.terms.note,
                    rest=RestOf(address=held.address, expires_at=held.expires_at),
                )
                await self._on_majority(pool, shop, event, sale, _REST)
            except SattlemeError as error:
                self.settings.logger.warning(
                    "No Lightning invoice of the rest of an underpayment (%s): asked again at "
                    "the next check",
                    error.message,
                )
                return None
            invoice = RestInvoice(
                bolt11=answer.pr,
                payment_hash=decoded.payment_hash,
                verify_url=answer.verify,
                sats=ask.sats,
                created_at=decoded.timestamp,
                expires_at=decoded.expires_at,
            )
            # Declared: it may be shown. Its record, for the checkout links' pages.
            recorded = await self._publish_rest_record(scope, held, invoice)
            return invoice, LightningResult("open"), recorded

    async def _publish_rest_record(self, scope: Scope, held: Held, invoice: RestInvoice) -> bool:
        """The rest record of `invoice` on a majority; a failure only logged."""
        keys = held.rest_keys(self.settings.key)
        try:
            event = rest_record_event(
                keys,
                invoice=invoice.bolt11,
                verify_url=invoice.verify_url,
                payment_hash=invoice.payment_hash,
                expires_at=held.expires_at,
                created_at=self.now(),
                random=self.settings.random,
            )
        except ValueError:
            self.settings.logger.warning(
                "An invoice of the rest is too large for its record: the checkout links' pages "
                "offer the rest on-chain only"
            )
            return False
        try:
            await scope.pool.publish(scope.shop.relays, event, key=keys.secret)
        except NetworkError as error:
            self.settings.logger.warning(
                "The record of an invoice of the rest was not published (%s): published again at "
                "the next check",
                error.message,
            )
            return False
        return True

    async def _record_rests(
        self, scope: Scope, held: Held, invoices: tuple[RestInvoice, ...], taken: set[str]
    ) -> set[str]:
        """Publish again the rest records that fewer than a majority of the relays sent, but for
        those a majority took in this check (`taken`: "It is then the status's bolt11", SPEC.md
        §31) and those whose rest record address holds a record of a newer format that a relay
        sent (a newer version recorded it: never written over, SPEC.md §12, and known): the
        payment hashes of those not known on a majority (all the others when fewer than a
        majority answered), which are never the status's `bolt11`. A failure is logged."""
        keys = held.rest_keys(self.settings.key)
        relays = scope.shop.relays

        def recorded(value: object) -> Opened[RestShown] | Newer | None:
            """The rest record a relay sent, as the server reads it (no link's bounds)."""
            return open_rest_record(value, keys, None)

        try:
            answers: dict[str, list[Opened[RestShown] | Newer]] = await scope.pool.read(
                relays, {"kinds": [RECORD_KIND], "authors": [keys.author]}, recorded
            )
        except NetworkError as error:
            self.settings.logger.warning(
                "The records of the invoices of the rest were not read (%s): read again at the "
                "next check",
                error.message,
            )
            return {i.payment_hash for i in invoices if i.payment_hash not in taken}
        sent: dict[str, int] = {}
        # The rest record addresses where a relay sent a record of a newer format.
        newer: set[str] = set()
        for records in answers.values():
            payment_hashes: set[str] = set()
            for opened in records:
                if isinstance(opened, Newer):
                    newer.add(opened.event.tag("d") or "")
                else:
                    payment_hashes.add(opened.value.payment_hash)
            for payment_hash in payment_hashes:
                sent[payment_hash] = sent.get(payment_hash, 0) + 1
        if newer:
            self.settings.logger.warning(
                "A record of an invoice of the rest has a newer format than this version of "
                "sattleme-checkout reads: update it"
            )
        unrecorded: set[str] = set()
        wanted = majority(len(relays))
        for invoice in invoices:
            if invoice.payment_hash in taken or sent.get(invoice.payment_hash, 0) >= wanted:
                continue
            if keys.record_address(invoice.payment_hash) in newer:
                # A newer version recorded it: never written over, it counts as known.
                continue
            if not await self._publish_rest_record(scope, held, invoice):
                unrecorded.add(invoice.payment_hash)
        return unrecorded

    async def _publish_acceptance(self, scope: Scope, held: Held, parts: Parts) -> None:
        """When the order's tolerance or the merchant's acceptance accepted less than its amount,
        have the pages of its checkout links know it (SPEC.md §31, Acceptance records): what was
        received, never the tolerance. A failure is logged, never the check's error."""
        now = self.now()
        try:
            status = status_of(held.stored, parts, now)
        except SattlemeError:
            return
        received = acceptance_received(status, sats=held.sats, expires_at=held.expires_at, now=now)
        if received is None:
            return
        keys = held.rest_keys(self.settings.key)
        relays = scope.shop.relays
        try:
            accepted: dict[str, list[Opened[int] | Newer]] = await scope.pool.read(
                relays,
                {
                    "kinds": [RECORD_KIND],
                    "authors": [keys.author],
                    "#d": [keys.acceptance_address],
                },
                lambda value: open_acceptance(value, keys, held.sats),
            )
            found, newer = newest_acceptances(accepted.values())
            if newer:
                # Never written over (SPEC.md §12): a newer version holds the place.
                self.settings.logger.warning(
                    "An acceptance record of this order has a newer format than this version of "
                    "sattleme-checkout reads: update it"
                )
                return
            if acceptance_known(found, received, len(relays)):
                return
            event = acceptance_event(
                keys,
                received=received,
                expires_at=held.expires_at,
                created_at=self.now(),
                random=self.settings.random,
            )
            await scope.pool.publish(relays, event, key=keys.secret)
        except NetworkError as error:
            self.settings.logger.warning(
                "The acceptance record of a payment was not published (%s): published again at "
                "the next check",
                error.message,
            )


_REST = "The declaration of an invoice of the rest"

Error = SattlemeError


def _order_paid(invoice: DeclaredInvoice, status: PaymentStatus) -> OrderPaidError:
    return OrderPaidError(
        "An earlier invoice of the order was paid",
        invoice=invoice,
        status=status,
        preimage=status.preimage if status.method == "lightning" else None,
    )


def _rest_to_ask(held: Held, parts: Parts, now: int) -> int | None:
    """The rest to ask an invoice of, when the status calls for one before the lease is read
    (SPEC.md §31, Asking for a rest invoice: open, a rest without an invoice, 60 s left)."""
    try:
        status = status_of(held.stored, parts, now)
    except SattlemeError:
        # A part failed: the status is its error, nothing is asked.
        return None
    rest = status.rest
    if rest is None or rest.bolt11 is not None or now + REST_MIN_LEFT > held.expires_at:
        return None
    return rest.sats
