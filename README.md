# KR Crypto Intelligence API

Korean crypto market data + AI analysis for AI agents. 10 endpoints, 180+ tokens. Pay-per-use via x402 protocol on Base and Solana.

## Endpoints (10)

### Korean Exchange Intelligence (NEW)
| Endpoint | Price | Description |
|----------|-------|-------------|
| `/api/v1/arbitrage-scanner` | $0.01 | Token-by-token Kimchi Premium for 180+ tokens, reverse premium, Upbit-Bithumb price gaps, market share |
| `/api/v1/exchange-alerts` | $0.01 | New listings/delistings detection, investment warnings, caution flags (volume soaring, deposit soaring, etc.) |
| `/api/v1/market-movers` | $0.01 | 1-min price surges/crashes, volume spikes, top 20 tokens by trading volume |

### AI-Powered Analysis
| Endpoint | Price | Description |
|----------|-------|-------------|
| `/api/v1/market-read` | $0.10 | AI market analysis — 12+ data sources + exchange intelligence + Claude AI token-level signals |

### Market Data
| Endpoint | Price | Description |
|----------|-------|-------------|
| `/api/v1/kimchi-premium` | $0.001 | BTC Kimchi Premium (Upbit vs Binance) |
| `/api/v1/stablecoin-premium` | $0.001 | USDT/USDC premium on Korean exchanges (fund flow indicator) |
| `/api/v1/kr-prices` | $0.001 | Korean exchange prices (Upbit, Bithumb) |
| `/api/v1/fx-rate` | $0.001 | USD/KRW exchange rate |

### Free
| Endpoint | Price | Description |
|----------|-------|-------------|
| `/api/v1/symbols` | Free | Available trading symbols |
| `/health` | Free | Service health check |

## Live API

**Base URL:** `https://api.printmoneylab.com`
**MCP Server:** `https://mcp.printmoneylab.com/mcp`
**API Docs:** [https://api.printmoneylab.com/docs](https://api.printmoneylab.com/docs)

## Key Features

### Arbitrage Scanner
- Real-time Kimchi Premium for **every token** traded on both Upbit and Binance (180+)
- Reverse premium detection (Korean discount = buy signal)
- Upbit vs Bithumb price gap scanner (domestic arbitrage)
- Market share tracking (Upbit vs Bithumb volume)

### Exchange Alerts
- New listing / delisting detection (market list comparison every 60s)
- Investment warning flags (from Upbit official API)
- Caution flags: price fluctuations, volume soaring, deposit soaring, global price differences, small account concentration

### Market Movers
- 1-minute price surge/crash detection (>1% in 60 seconds)
- Volume spike detection (24h change rate, >1B KRW volume)
- Top 20 tokens by trading volume

### AI Market Read
- 12+ data sources combined: Kimchi Premium, stablecoin premium, FX rate, Upbit/Bithumb volume TOP 5, BTC funding rate, open interest, dominance, Fear & Greed, + exchange intelligence (180+ tokens)
- Claude AI generates: signal (BULLISH/BEARISH/NEUTRAL), confidence (1-10), summary, key factors, **token-level alerts**, risk warning

## Payment

Uses the [x402 protocol](https://x402.org) for micropayments. No API key, no subscription, no signup required.
- **Base:** USDC on Base mainnet (eip155:8453)
- **Solana:** USDC on Solana mainnet

## Data Sources

- **Upbit** — Largest Korean crypto exchange (245+ KRW markets)
- **Bithumb** — Second largest Korean exchange (450+ markets)
- **Binance** — Global price reference (659+ USDT markets)
- **exchangerate-api.com** — USD/KRW FX rate
- **CoinGecko** — BTC/ETH/ALT dominance
- **Alternative.me** — Fear & Greed Index
- **Binance Futures** — Funding rate, open interest
- **Claude AI (Haiku 4.5)** — Market analysis

## MCP Server

Connect any MCP-compatible AI agent (Claude, Cursor, etc.):

```json
{
  "mcpServers": {
    "kr-crypto-intelligence": {
      "url": "https://mcp.printmoneylab.com/mcp"
    }
  }
}
```

10 tools available: `get_kimchi_premium`, `get_kr_prices`, `get_fx_rate`, `get_stablecoin_premium`, `get_available_symbols`, `check_health`, `get_market_read`, `get_arbitrage_scanner`, `get_exchange_alerts`, `get_market_movers`

## Tech Stack

- Python / FastAPI
- x402 Payment Protocol (Base + Solana USDC)
- CDP Facilitator (Coinbase Developer Platform)
- Claude AI (Haiku 4.5) for market analysis
- FastMCP (Streamable HTTP transport)
- Cloudflare SSL
- Oracle Cloud ARM (Always Free Tier, $0/month)

## License

MIT
