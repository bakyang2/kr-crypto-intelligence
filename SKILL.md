---
name: kr-crypto-intelligence
description: Korean crypto market data + AI analysis for trading agents. 13 tools — dual-basis Kimchi Premium (official USD/KRW + USDT live rate) across 180+ tokens, exchange intelligence, AI sentiment analysis (world's first Korean-to-English), Global vs Korea divergence with structured AI breakdown, market alerts. x402 on Base, Polygon, and Solana.
version: 1.4.2
homepage: https://github.com/bakyang2/kr-crypto-intelligence
repository: https://github.com/bakyang2/kr-crypto-intelligence
metadata:
  clawdbot:
    emoji: "🇰🇷"
    requires:
      env: []
---

# KR Crypto Intelligence

Korean crypto market data API + AI-powered market analysis for trading agents. South Korea ranks top 3 globally in crypto trading volume. 13 tools covering 180+ tokens.

## How to Use

MCP server — no local code, no API keys, no credentials needed.

### MCP Connection

```json
{
  "mcpServers": {
    "kr-crypto-intelligence": {
      "url": "https://mcp.printmoneylab.com/mcp"
    }
  }
}
```

### Available Tools (13)

| Tool | Price | Description |
|------|-------|-------------|
| `get_market_read` | **$0.10** | AI market analysis — 12+ sources + exchange intelligence + Claude AI token-level signals |
| `get_global_vs_korea_divergence_deep` | **$0.10** | Deep tier — light data + Korean news signals (Coinness Telegram) + structured AI breakdown |
| `get_global_vs_korea_divergence` | $0.05 | Light tier — global vs Korean price premium + AI interpretation |
| `get_kr_sentiment` | $0.05 | Korean market sentiment in English — exchange intelligence + Korean news context |
| `get_arbitrage_scanner` | $0.01 | Token-by-token Kimchi Premium for 180+ tokens, reverse premium, Upbit-Bithumb gaps |
| `get_exchange_alerts` | $0.01 | New listings/delistings, investment warnings, caution flags |
| `get_market_movers` | $0.01 | 1-min price surges/crashes, volume spikes, top 20 by volume |
| `get_kimchi_premium` | $0.001 | **Dual-basis** Kimchi Premium — `premium_percent` (official USD/KRW) + `premium_pct_usdt` (Upbit USDT live rate). Gap = real arbitrage margin |
| `get_stablecoin_premium` | $0.001 | USDT/USDC premium (fund flow indicator) |
| `get_kr_prices` | $0.001 | KRW prices from Upbit/Bithumb |
| `get_fx_rate` | $0.001 | USD/KRW exchange rate |
| `get_available_symbols` | $0.001 | Tradeable symbols list |
| `check_health` | $0.001 | Service status |

### REST API (Alternative)
GET https://api.printmoneylab.com/api/v1/market-read                     → $0.10
GET https://api.printmoneylab.com/api/v1/global-vs-korea-divergence-deep → $0.10
GET https://api.printmoneylab.com/api/v1/global-vs-korea-divergence      → $0.05
GET https://api.printmoneylab.com/api/v1/kr-sentiment                    → $0.05
GET https://api.printmoneylab.com/api/v1/arbitrage-scanner               → $0.01
GET https://api.printmoneylab.com/api/v1/exchange-alerts                 → $0.01
GET https://api.printmoneylab.com/api/v1/market-movers                   → $0.01
GET https://api.printmoneylab.com/api/v1/kimchi-premium                  → $0.001
GET https://api.printmoneylab.com/api/v1/stablecoin-premium              → $0.001
GET https://api.printmoneylab.com/api/v1/kr-prices                       → $0.001
GET https://api.printmoneylab.com/api/v1/fx-rate                         → $0.001
GET https://api.printmoneylab.com/api/v1/symbols                         (free)
GET https://api.printmoneylab.com/health                                 (free)

## Data Privacy & What Gets Sent

**The server requires only the tool call parameters listed below. What your MCP client actually transmits depends on the client implementation — please review your MCP client's privacy and transport settings to verify.**

Specifically, each tool requires:
- `get_kimchi_premium`, `get_global_vs_korea_divergence`, `get_global_vs_korea_divergence_deep`: `symbol` parameter only (e.g., "BTC")
- `get_kr_prices`: `symbol` and `exchange` parameters only
- `get_arbitrage_scanner`, `get_exchange_alerts`, `get_market_movers`: no parameters — server computes from cached exchange data
- `get_market_read`: no parameters — server fetches all data internally and runs AI analysis server-side
- `get_kr_sentiment`: no parameters — server combines exchange data with Korean news context and runs AI sentiment analysis server-side
- `get_fx_rate`, `get_stablecoin_premium`, `get_available_symbols`, `check_health`: no parameters

Note: Like any HTTP service, the server receives standard HTTP metadata (IP address, user-agent) as part of normal network communication.

**Network calls only to:** `mcp.printmoneylab.com` and `api.printmoneylab.com`

## Payment Authorization (x402 Protocol)

**How x402 payment works — step by step:**

1. Agent calls a paid endpoint (e.g., `get_kimchi_premium`)
2. Server returns HTTP 402 with price in the `payment-required` header
3. **The MCP client or platform decides whether to pay** — this is NOT automatic
4. If the client approves, it signs a USDC transfer for the exact amount on Base, Polygon, or Solana
5. Client retries with payment proof in `X-PAYMENT` header
6. Server verifies payment and returns data

**Key points:**
- **Payment is NOT automatic.** The agent's MCP client (e.g., xpay Smart Proxy, Claude, Cursor) controls whether to authorize payment.
- **No wallet keys or credentials are stored in this skill.** Payment is handled entirely by the MCP client's x402 transport layer.
- **The skill cannot charge without explicit client-side authorization.** The x402 protocol requires a cryptographic signature from the buyer's wallet.
- **Cost per call:** $0.001 (basic data) to $0.10 (AI deep analysis). No subscriptions, no hidden fees.

## Autonomous Invocation Advisory

This skill is designed to be invoked by the agent when the user asks about Korean crypto markets. If your platform supports invocation controls:
- **Recommended:** Set to "user-invoked only" until comfortable with billing behavior
- **Budget:** Configure your MCP client's spending limit
- **Maximum cost per session:** Bounded by your client's spending policy

## Security

- **No local code execution** — instruction-only skill
- **No credentials stored** — no API keys, no wallet keys, no env vars
- **No file system access** — all data from remote API
- **Open source:** https://github.com/bakyang2/kr-crypto-intelligence (MIT license)
- **API docs:** https://api.printmoneylab.com/docs (Swagger/OpenAPI)
- **Registered on:** Official MCP Registry, Glama, Smithery, xpay.tools, awesome-x402, awesome-mcp-servers
