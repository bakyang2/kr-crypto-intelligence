name: kr-crypto-intelligence
description: Korean crypto market data + AI analysis for trading agents. 10 endpoints, 180+ tokens. Real-time Kimchi Premium for all tokens, exchange intelligence (warnings, listings, volume spikes), AI market read with token-level signals. x402 on Base and Solana.
---
# KR Crypto Intelligence

## Overview
Korean crypto market data + AI analysis API for AI agents. South Korea ranks top 3 globally in crypto trading volume. 10 endpoints covering 180+ tokens.

## Endpoints
Base URL: `https://api.printmoneylab.com`

### Korean Exchange Intelligence ($0.01/call)
| Endpoint | Description |
|----------|-------------|
| `/api/v1/arbitrage-scanner` | Token-by-token Kimchi Premium for 180+ tokens, reverse premium, Upbit-Bithumb gaps, market share |
| `/api/v1/exchange-alerts` | New listings/delistings, investment warnings, caution flags (volume soaring, deposit soaring, etc.) |
| `/api/v1/market-movers` | 1-min price surges/crashes, volume spikes, top 20 by volume |

### AI Analysis ($0.10/call)
| Endpoint | Description |
|----------|-------------|
| `/api/v1/market-read` | AI market analysis — 12+ sources + exchange intelligence + Claude AI token-level signals |

### Market Data ($0.001/call)
| Endpoint | Description |
|----------|-------------|
| `/api/v1/kimchi-premium?symbol=BTC` | BTC Kimchi Premium (Upbit vs Binance) |
| `/api/v1/stablecoin-premium` | USDT/USDC premium — capital flow indicator |
| `/api/v1/kr-prices?symbol=BTC` | Korean exchange prices (Upbit, Bithumb) |
| `/api/v1/fx-rate` | USD/KRW exchange rate |

### Free
| Endpoint | Description |
|----------|-------------|
| `/api/v1/symbols` | Available trading symbols |
| `/health` | Service health check |

## MCP Server
URL: `https://mcp.printmoneylab.com/mcp`

10 tools: `get_kimchi_premium`, `get_kr_prices`, `get_fx_rate`, `get_stablecoin_premium`, `get_available_symbols`, `check_health`, `get_market_read`, `get_arbitrage_scanner`, `get_exchange_alerts`, `get_market_movers`

## Payment
x402 protocol — no API key, no subscription, no signup.
- Base: USDC on eip155:8453
- Solana: USDC on mainnet

## Example
```python
# Arbitrage scanner — all 180+ tokens
from x402 import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.http.clients.httpx import x402HttpxClient

client = x402Client()
client.register("eip155:8453", ExactEvmScheme(signer=your_signer))
httpx_client = x402HttpxClient(client)
r = await httpx_client.get("https://api.printmoneylab.com/api/v1/arbitrage-scanner")
# Returns 180+ tokens with premium_pct, warning flags, volume data
```
