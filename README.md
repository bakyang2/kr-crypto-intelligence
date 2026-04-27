# KR Crypto Intelligence API

Korean crypto market data + AI analysis for AI agents. 11 paid endpoints, 180+ tokens, world's first Korean-to-English crypto sentiment API. Pay-per-use via x402 protocol on Base, Polygon, and Solana.

## Endpoints (11 paid)

### Korean Sentiment Analysis (World's First)
| Endpoint | Price | Description |
|----------|-------|-------------|
| `/api/v1/kr-sentiment` | $0.05 | Korean market sentiment in English — combines exchange intelligence (189+ tokens) with Korean news context (Coinness Telegram) for AI-powered insights. 1-hour cache. |

### Global vs Korea Divergence (NEW)
| Endpoint | Price | Description |
|----------|-------|-------------|
| `/api/v1/global-vs-korea-divergence` | $0.05 | Global vs Korea divergence (light) — CoinGecko global price + Korean exchange + 1-2 sentence AI summary. 60s cache. |
| `/api/v1/global-vs-korea-divergence-deep` | $0.10 | Deep tier — light data + Korean news signals (Coinness Telegram) + structured AI breakdown (drivers, global context, action suggestion, confidence). 5-min cache. |

### Korean Exchange Intelligence
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
| Endpoint | Description |
|----------|-------------|
| `/api/v1/symbols` | Available trading symbols |
| `/api/v1/stats` | Service statistics |
| `/health` | Service health check |
| `/llms.txt` | AI agent metadata |
| `/.well-known/x402` | x402 service discovery |

## Live API

**Base URL:** `https://api.printmoneylab.com`
**MCP Server:** `https://mcp.printmoneylab.com/mcp`
**API Docs:** [https://api.printmoneylab.com/docs](https://api.printmoneylab.com/docs)

## Key Features

### KR Sentiment (Unique — No Competitors Worldwide)
- Real-time Korean market sentiment delivered in English
- Combines exchange data (189+ tokens Kimchi Premium, warnings, volume spikes, deposit soaring) with Korean news context (Coinness Telegram, 6-hour window)
- Claude AI generates: sentiment (BULLISH/BEARISH/CAUTIOUS_FOMO/PANIC/GREED/UNCERTAIN), score (-1.0 to +1.0), English report, exchange signals, news context, sources
- Academic research validates: "Korean news sentiment predicts global crypto returns" (2026)
- 1-hour cache + lazy invocation + concurrency lock for cost efficiency

### Global vs Korea Divergence
- Light tier: real-time premium between CoinGecko global price and Korean exchange (Upbit), with 1-2 sentence AI interpretation
- Deep tier: adds Korean news signals (Coinness Telegram, 24h keywords + sentiment score) and structured AI analysis with Korean market drivers, global context, action suggestion, and confidence level
- 25 supported symbols (BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB)

### Arbitrage Scanner
- Real-time Kimchi Premium for **every token** traded on both Upbit and Binance (189+)
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
- **Polygon:** USDC on Polygon mainnet (eip155:137)
- **Solana:** USDC on Solana mainnet

## Data Sources

- **Upbit** — Largest Korean crypto exchange (245+ KRW markets)
- **Bithumb** — Second largest Korean exchange (450+ markets)
- **Binance** — Global price reference (659+ USDT markets)
- **CoinGecko** — Global crypto prices (divergence endpoints)
- **Coinness Telegram** — Korean crypto news source for sentiment analysis
- **exchangerate-api.com** — USD/KRW FX rate
- **Alternative.me** — Fear & Greed Index
- **Binance Futures** — Funding rate, open interest
- **Claude AI (Haiku 4.5)** — Market analysis and sentiment interpretation

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

## License

MIT License — see [LICENSE](./LICENSE)
