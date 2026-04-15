---
name: kr-crypto-intelligence
description: Korean crypto market data + AI analysis for trading agents. 10 endpoints, 180+ tokens. Real-time Kimchi Premium for all tokens, exchange intelligence (warnings, listings, volume spikes), AI market read with token-level signals. x402 on Base and Solana.
---

# KR Crypto Intelligence

## Overview
Korean crypto market data + AI analysis API for AI agents. South Korea ranks top 3 globally in crypto trading volume. 10 endpoints covering 180+ tokens.

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

### Available Tools (10)

#### Korean Exchange Intelligence ($0.01/call)
| Tool | Description |
|------|-------------|
| `get_arbitrage_scanner` | Token-by-token Kimchi Premium for 180+ tokens, reverse premium, Upbit-Bithumb gaps, market share |
| `get_exchange_alerts` | New listings/delistings, investment warnings, caution flags |
| `get_market_movers` | 1-min price surges/crashes, volume spikes, top 20 by volume |

#### AI Analysis ($0.10/call)
| Tool | Description |
|------|-------------|
| `get_market_read` | AI market analysis — 12+ sources + exchange intelligence + token-level signals |

#### Market Data ($0.001/call)
| Tool | Description |
|------|-------------|
| `get_kimchi_premium` | BTC Kimchi Premium (Upbit vs Binance) |
| `get_stablecoin_premium` | USDT/USDC premium — capital flow indicator |
| `get_kr_prices` | Korean exchange prices (Upbit, Bithumb) |
| `get_fx_rate` | USD/KRW exchange rate |
| `get_available_symbols` | Tradeable symbols list |
| `check_health` | Service status |

### REST API (Alternative)

Base URL: `https://api.printmoneylab.com`
GET /api/v1/arbitrage-scanner       → $0.01
GET /api/v1/exchange-alerts         → $0.01
GET /api/v1/market-movers           → $0.01
GET /api/v1/market-read             → $0.10
GET /api/v1/kimchi-premium          → $0.001
GET /api/v1/stablecoin-premium      → $0.001
GET /api/v1/kr-prices               → $0.001
GET /api/v1/fx-rate                 → $0.001
GET /api/v1/symbols                 (free)
GET /health                         (free)

## Data Privacy & What Gets Sent

**This skill sends ONLY tool call parameters to the server. No conversation context, no user data, no prompts are forwarded.**

Specifically, each tool sends:
- `get_kimchi_premium`: `symbol` parameter only (e.g., "BTC")
- `get_kr_prices`: `symbol` and `exchange` parameters only
- `get_arbitrage_scanner`, `get_exchange_alerts`, `get_market_movers`: no parameters — server computes from cached exchange data
- `get_market_read`: no parameters — server fetches all data internally and runs AI analysis server-side
- `get_fx_rate`, `get_stablecoin_premium`, `get_available_symbols`, `check_health`: no parameters

The server does NOT receive or store:
- Agent conversation history or user prompts
- User identity or account information
- Any data beyond the explicit tool parameters listed above

**Network calls only to:** `mcp.printmoneylab.com` and `api.printmoneylab.com`

## Payment Authorization (x402 Protocol)

**How x402 payment works — step by step:**

1. Agent calls a paid endpoint (e.g., `get_arbitrage_scanner`)
2. Server returns HTTP 402 with price in the header
3. **The MCP client or platform decides whether to pay** — this is NOT automatic
4. If the client approves, it signs a USDC transfer for the exact amount
5. Client retries with payment proof in header
6. Server verifies payment and returns data

**Key points:**
- **Payment is NOT automatic.** The agent's MCP client controls whether to authorize payment.
- **No wallet keys or credentials are needed by users.** Payment is handled entirely by the MCP client's x402 transport layer.
- **The server cannot charge without explicit client-side authorization.** The x402 protocol requires a cryptographic signature from the buyer's wallet.
- **No API keys, no credentials, no env vars needed.** Everything is handled server-side or by the x402 protocol.

## Autonomous Invocation Advisory

This skill is designed to be invoked by the agent when the user asks about Korean crypto markets. If your platform supports invocation controls:
- **Recommended:** Set to "user-invoked only" until comfortable with billing behavior
- **Budget:** Configure your MCP client's spending limit
- **Maximum cost per session:** Bounded by your client's spending policy

## Security

- **No local code execution** — instruction-only skill
- **No credentials required** — no API keys, no wallet keys, no env vars
- **No file system access** — all data from remote API
- **Open source:** https://github.com/bakyang2/kr-crypto-intelligence (MIT license)
- **API docs:** https://api.printmoneylab.com/docs (Swagger/OpenAPI)
- **Registered on:** Official MCP Registry, Glama (AAA 100), Smithery, xpay.tools, ClawHub, awesome-x402, awesome-mcp-servers
