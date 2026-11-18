"""Every constant of SPEC.md §28, and the few the sections around it define."""

from typing import Final

PROTOCOL_VERSION: Final = 1

# SPEC.md §2: more arrays and objects than this nesting one in another is not JSON.
JSON_DEPTH_MAX: Final = 64

KEY_PREFIX: Final = "sattleme_ck_"
RECORD_KIND: Final = 30078
AUTH_KIND: Final = 22242
RECORD_PADDING: Final = 256

CHANNEL_TYPE: Final = "team-channel"
CHANNEL_VERSION: Final = 1
DECLARATION_TYPE: Final = "registry-declaration"
DECLARATION_VERSION: Final = 1
SATS_CURRENCY: Final = "SAT"

RELAY_MOVES: Final = 3
RELAYS_MIN: Final = 1
RELAYS_MAX: Final = 10
RELAY_URL_MAX: Final = 120
HOST_MAX: Final = 253
BUSINESS_NAME_MAX: Final = 200
REF_MAX: Final = 64
DURATION_MIN: Final = 60
DURATION_MAX: Final = 3600
DURATION_DEFAULT: Final = 900
NOTE_MAX: Final = 100
GRACE_SECONDS: Final = 120
CLOCK_TOLERANCE: Final = 300
RATE_AGE: Final = 3600
PRICE_TEXT_MAX: Final = 64
PRICE_EXPONENT_MAX: Final = 64
EVENTS_PER_QUERY: Final = 1000
PAGES_MAX: Final = 20
COMPLETE_READ_LIMIT: Final = 100
MESSAGE_MAX_BYTES: Final = 1 << 20
HTTP_BODY_MAX_BYTES: Final = 1 << 20
# An answer's head: its status line, headers and a chunked body's size lines (SPEC.md §15).
HTTP_HEAD_MAX_BYTES: Final = 10 << 20
PAYMENT_CHECKS_AT_ONCE: Final = 4
TIMEOUT_DEFAULT_MS: Final = 10_000
TIMEOUT_MIN_MS: Final = 100
TIMEOUT_MAX_MS: Final = 60_000
INTERVAL_DEFAULT_MS: Final = 2000
INTERVAL_MIN_MS: Final = 250
INTERVAL_MAX_MS: Final = 60_000
BOLT11_EXPIRY_WORDS_MAX: Final = 7
BOLT11_EXPIRY_DEFAULT: Final = 3600
BOLT11_AMOUNT_MAX_MSAT: Final = 2_100_000_000_000_000_000
MINOR_MAX: Final = 9_007_199_254_740_991
SAFE_INTEGER_MAX: Final = 9_007_199_254_740_991
SATS_MAX: Final = 2_100_000_000_000_000

CHECKOUT_SALT: Final = "sattle.me/checkout"
CHECKOUT_TYPE: Final = "checkout"
CHECKOUT_VERSION: Final = 1
CHECKOUT_VERSION_ONCHAIN: Final = 2
CHECKOUT_MARKER: Final = "c"
CHECKOUT_LIFE: Final = 604_800
CHECKOUT_PATH: Final = "/pay"
REDIRECT_URL_MAX: Final = 2048

ONCHAIN_FLOOR: Final = 1000
LOT_TYPE: Final = "integration-onchain-lot"
LOT_VERSION: Final = 1
LOT_SIZE_MAX: Final = 100
LEASE_TYPE: Final = "registry-onchain"
LEASE_VERSION: Final = 1
LEASE_LOOKBACK: Final = 7200
ADDRESSES_TRIED: Final = 3
WITNESS_TYPE: Final = "integration-onchain-seen"
WITNESS_VERSION: Final = 1
WITNESS_INPUTS_MAX: Final = 100
CONFIRMATIONS: Final = 3
REST_TYPE: Final = "checkout-rest"
REST_VERSION: Final = 1
REST_MIN_LEFT: Final = 60
TOLERANCE_MAX: Final = 1000
ACCEPTANCE_TYPE: Final = "checkout-accepted"
ACCEPTANCE_VERSION: Final = 1
MERCHANT_ACCEPTANCE_TYPE: Final = "integration-onchain-accepted"
MERCHANT_ACCEPTANCE_VERSION: Final = 1

INTEGRATION_SALT: Final = "sattle.me/integration"
SALE_SALT: Final = "sattle.me/sale"
DECLARATION_SALT: Final = "sattle.me/registry-declaration"

# mempool.space (SPEC.md §31, Explorers): the explorer, unless the channel names the merchant's
# Bitcoin node, and the node's fallback when it says "mempool".
MEMPOOL: Final = "https://mempool.space/api"
MEMPOOL_WEBSOCKET: Final = "wss://mempool.space/api/v1/ws"
BLOCKCYPHER: Final = "https://api.blockcypher.com/v1/btc/main"
EXPLORER_CHECK_SECONDS: Final = 60
# The merchant's Bitcoin node (SPEC.md §31): its URL, and how long a node that failed a read is
# asked after mempool.space (`fallback` "mempool"), in the same call.
NODE_URL_MAX: Final = 200
NODE_FAILED_RECENTLY_SECONDS: Final = 60
RECONNECT_FIRST_SECONDS: Final = 1
RECONNECT_MAX_SECONDS: Final = 30
