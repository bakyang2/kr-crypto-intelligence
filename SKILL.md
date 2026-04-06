---
name: kr-crypto-intelligence
description: Access Korean crypto market data — Kimchi Premium, stablecoin premium, Upbit/Bithumb prices, USD/KRW FX rate. Use when building agents that need Korean exchange data, capital flow signals, or cross-market arbitrage detection.
---

# KR Crypto Intelligence

## Overview

Korean crypto market data API for AI agents. South Korea ranks top 3 globally in crypto trading volume, making Korean market signals valuable for global trading strategies.

## Endpoints

All endpoints at `https://api.printmoneylab.com`, paid via x402 ($0.001 USDC on Base).

| Endpoint | Description |
|----------|-------------|
| `/api/v1/kimchi-premium?symbol=BTC` | Price gap between Korean exchanges (Upbit) and global (Binance) |
| `/api/v1/stablecoin-premium` | USDT/USDC premium on Korean exchanges vs official FX rate — capital flow indicator |
| `/api/v1/kr-prices?symbol=BTC&exchange=all` | Upbit/Bithumb KRW prices, 24h volume, change rate |
| `/api/v1/fx-rate` | Live USD/KRW exchange rate |
| `/api/v1/symbols` | Available trading symbols (free) |

## MCP Server

SSE endpoint: `https://mcp.printmoneylab.com/sse`

Tools: `get_kimchi_premium`, `get_kr_prices`, `get_fx_rate`, `get_stablecoin_premium`, `get_available_symbols`, `check_health`

## Example Usage
```python
import httpx

# Free endpoint
r = httpx.get("https://api.printmoneylab.com/api/v1/symbols")
symbols = r.json()

# x402 paid endpoint
from x402 import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.http.clients.httpx import x402HttpxClient

client = x402Client()
client.register("eip155:8453", ExactEvmScheme(signer=your_signer))
httpx_client = x402HttpxClient(client)

r = await httpx_client.get("https://api.printmoneylab.com/api/v1/kimchi-premium?symbol=BTC")
# {"symbol": "BTC", "premium_percent": 0.23, "premium_direction": "positive", ...}
```

## Key Signals

- **Kimchi Premium > 3%**: Strong local demand, potential arbitrage opportunity
- **Stablecoin Premium positive**: Capital flowing INTO Korean crypto market
- **Stablecoin Premium negative**: Capital flowing OUT — risk-off signal

## Data Sources

- Upbit (Korea #1 exchange)
- Bithumb (Korea #2 exchange)
- Binance (global reference)
- exchangerate-api.com (FX rate)

## Links

- API Docs: https://api.printmoneylab.com/docs
- GitHub: https://github.com/bakyang2/kr-crypto-intelligence
- x402 Manifest: https://api.printmoneylab.com/.well-known/x402
