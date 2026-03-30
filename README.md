# KR Crypto Intelligence API

Korean crypto market data for AI agents. Pay-per-use via x402 protocol on Base.

## Endpoints

| Endpoint | Description | Price |
|----------|------------|-------|
| `/api/v1/kimchi-premium` | Real-time Kimchi Premium (Upbit vs Binance) | $0.001 USDC |
| `/api/v1/kr-prices` | Korean exchange prices (Upbit, Bithumb) | $0.001 USDC |
| `/api/v1/fx-rate` | USD/KRW exchange rate | $0.001 USDC |
| `/api/v1/symbols` | Available trading symbols | Free |
| `/api/v1/stats` | API usage statistics | Free |
| `/health` | Service health check | Free |

## Live API

**Base URL:** `https://api.printmoneylab.com`

**Health Check:** [https://api.printmoneylab.com/health](https://api.printmoneylab.com/health)

**API Docs:** [https://api.printmoneylab.com/docs](https://api.printmoneylab.com/docs)

## What is Kimchi Premium?

The Kimchi Premium is the price difference between Korean crypto exchanges (Upbit, Bithumb) and global exchanges (Binance). South Korea ranks top 3 globally in crypto trading volume, making Korean market data valuable for global AI trading agents.

## Payment

Uses the [x402 protocol](https://x402.org) for micropayments. AI agents pay $0.001 USDC per request on Base network. No API key, no subscription, no signup required.

## Data Sources

- **Upbit** — Largest Korean crypto exchange
- **Bithumb** — Second largest Korean exchange
- **Binance** — Global price reference
- **exchangerate-api.com** — USD/KRW FX rate

## Tech Stack

- Python / FastAPI
- x402 Payment Protocol (Base mainnet USDC)
- Cloudflare SSL
- Oracle Cloud ARM (Always Free Tier)

## License

MIT
