"""Public program ids, Anchor discriminators, and protocol constants.

Everything here is public chain data or a published protocol constant. There are
no keys, credentials, or private endpoints in this module.
"""

from __future__ import annotations

# The pump.fun bonding-curve program. One `logsSubscribe` that mentions this
# program receives every trade and every launch on the curve.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# The PumpSwap AMM that graduated coins migrate to.
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Anchor event discriminators: first eight bytes of sha256("event:<Name>").
DISC_TRADE = bytes.fromhex("bddb7fd34ee661ee")       # TradeEvent
DISC_CREATE = bytes.fromhex("1b72a94ddeeb6376")      # CreateEvent
DISC_COMPLETE = bytes.fromhex("5f72619cd42e9808")    # CompleteEvent

# Free public Solana WebSocket endpoints. The first is operated by the Solana
# Foundation; the second is a community mirror. Override with PUMPFIRE_WS.
PUBLIC_WS = (
    "wss://api.mainnet-beta.solana.com",
    "wss://solana-rpc.publicnode.com",
)

# pump.fun's bonding curve uses six token decimals and a fixed 1B supply.
TOKEN_DECIMALS = 6
TOKEN_SCALE = 10**TOKEN_DECIMALS
LAMPORTS_PER_SOL = 1_000_000_000

# Launch state. These are the values the pump.fun program initialises a curve
# with; the CreateEvent carries them on newer launches and we fall back to these
# for older ones.
TOTAL_SUPPLY = 1_000_000_000.0
INITIAL_VIRTUAL_SOL = 30.0
INITIAL_VIRTUAL_TOKEN = 1_073_000_000.0
INITIAL_REAL_TOKEN = 793_100_000.0
GRADUATION_REAL_SOL = 85.0

# A leg below this size is dust or an internal transfer, not a trade.
MIN_LEG_SOL = 0.0005
# A leg above this size is a migration, a whale, or a mistake. Keep it by
# default, but the census and filters can use this bound.
MAX_LEG_SOL = 10_000.0

# The public endpoint rate-limits; a gap longer than this is reported as stale.
DEFAULT_STALE_AFTER_S = 45.0

# Maximum number of wallets remembered per coin for exact unique counts.
UNIQUE_WALLET_CAP = 5_000
