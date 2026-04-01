# Contributing to KR Crypto Intelligence API

Thank you for your interest in contributing to the KR Crypto Intelligence API! This project provides Korean crypto market data for AI agents via x402 pay-per-use protocol on Base.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [How to Contribute](#how-to-contribute)
- [Adding New Endpoints](#adding-new-endpoints)
- [Testing](#testing)
- [Submitting Changes](#submitting-changes)
- [Community](#community)

## Code of Conduct

This project adheres to a standard code of conduct. By participating, you are expected to:

- Be respectful and inclusive
- Accept constructive criticism gracefully
- Focus on what is best for the community
- Show empathy towards others

## Getting Started

### Prerequisites

- Python 3.9+
- pip or uv package manager
- Git
- A Base wallet (for testing x402 payments)
- (Optional) Telegram bot token for notifications

### Quick Start

1. **Fork the repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/kr-crypto-intelligence.git
   cd kr-crypto-intelligence
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

5. **Run the development server**
   ```bash
   uvicorn main:app --reload --port 8000
   ```

## Development Setup

### Environment Variables

Create a `.env` file with the following variables:

```env
# Required for x402 payments
X402_FACILITATOR_URL=https://x402.org/facilitator
X402_ADDRESS=0xYourWalletAddress

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional: Custom stats file path
STATS_FILE=./stats.json
```

### Understanding x402 Integration

The API uses the x402 protocol for micropayments on Base:

- **Free endpoints**: `/health`, `/api/v1/symbols`, `/api/v1/stats`
- **Paid endpoints**: All other endpoints require $0.001 USDC per request
- **Payment flow**: 
  1. Client requests paid endpoint without payment
  2. Server responds with 402 Payment Required + x402 headers
  3. Client signs payment and retries with X-Payment header
  4. Server verifies payment via facilitator and returns data

## Project Structure

```
kr-crypto-intelligence/
├── main.py              # FastAPI application with x402 middleware
├── README.md            # Project documentation
├── requirements.txt     # Python dependencies
├── .gitignore          # Git ignore rules
└── stats.json          # API usage statistics (auto-generated)
```

### Key Components

- **Caching System**: In-memory cache with TTL for exchange data
- **Rate Limiting**: Per-IP rate limiting to prevent abuse
- **x402 Middleware**: Payment verification for premium endpoints
- **Telegram Integration**: Real-time notifications for API usage
- **Statistics Tracking**: Request counts and revenue tracking

## How to Contribute

### Reporting Bugs

1. Check if the bug has already been reported in [Issues](../../issues)
2. If not, create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Your environment (Python version, OS)
   - Any error messages or logs

### Suggesting Enhancements

1. Open an issue with the `enhancement` label
2. Describe the feature and its use case
3. Explain why it would be valuable for AI agents

### Adding New Data Sources

The API currently supports:
- Upbit (Korean exchange)
- Bithumb (Korean exchange)
- Binance (Global reference)

To add a new exchange:

1. Add the exchange API endpoint to the `EXCHANGE_APIS` dictionary
2. Implement data normalization in the appropriate endpoint
3. Update the cache key format if needed
4. Add tests for the new exchange
5. Update documentation

## Adding New Endpoints

### Template for New Endpoint

```python
@app.get("/api/v1/your-endpoint")
async def your_endpoint(
    symbol: str = Query(..., description="Trading pair symbol"),
    request: Request = None
):
    """
    Description of what this endpoint does.
    
    - **symbol**: Trading pair (e.g., BTC-USDT)
    - Returns: Description of return value
    """
    cache_key = f"your_endpoint:{symbol}"
    
    # Check cache
    if cache_key in cache:
        data, timestamp = cache[cache_key]
        if time.time() - timestamp < CACHE_TTL:
            return {"data": data, "cached": True}
    
    # Fetch fresh data
    try:
        data = await fetch_your_data(symbol)
        cache[cache_key] = (data, time.time())
        return {"data": data, "cached": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### Adding x402 Payment to New Endpoints

Add your endpoint to the `protected_routes` list in the PaymentMiddlewareASGI:

```python
protected_routes=[
    RouteConfig(method="GET", path="/api/v1/kimchi-premium", price="$0.001"),
    RouteConfig(method="GET", path="/api/v1/your-endpoint", price="$0.001"),
]
```

## Testing

### Manual Testing

1. **Test free endpoints**:
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/api/v1/symbols
   ```

2. **Test paid endpoints** (requires x402 client):
   ```bash
   # This will return 402 Payment Required
   curl http://localhost:8000/api/v1/kimchi-premium?symbol=BTC-USDT
   ```

3. **Test with x402 payment**:
   Use an x402-compatible client or the x402 CLI to make paid requests.

### Load Testing

```bash
# Install hey for load testing
# On macOS: brew install hey
# On Linux: go install github.com/rakyll/hey@latest

hey -n 1000 -c 10 http://localhost:8000/api/v1/symbols
```

## Submitting Changes

1. **Create a branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Follow existing code style
   - Add comments for complex logic
   - Update README if adding new endpoints

3. **Test your changes**
   - Verify all existing endpoints still work
   - Test new functionality thoroughly
   - Check for any performance impacts

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "feat: add description of your changes"
   ```

   Commit message format:
   - `feat:` New feature
   - `fix:` Bug fix
   - `docs:` Documentation changes
   - `refactor:` Code refactoring
   - `perf:` Performance improvements
   - `test:` Adding tests

5. **Push and create PR**
   ```bash
   git push origin feature/your-feature-name
   ```
   Then create a Pull Request on GitHub.

### PR Checklist

- [ ] Code follows project style
- [ ] Changes are tested locally
- [ ] README updated if needed
- [ ] Commit messages are clear
- [ ] No unnecessary files included
- [ ] Environment variables documented

## Community

### For AI Agents

This API is designed specifically for AI agents. If you're an autonomous agent:

1. **Get started quickly**: Use the free `/api/v1/symbols` endpoint to see available pairs
2. **Understand pricing**: Each paid request costs $0.001 USDC on Base
3. **Handle 402 responses**: Implement x402 payment flow in your HTTP client
4. **Cache responses**: Respect the cache headers to minimize costs
5. **Monitor usage**: Check `/api/v1/stats` to track your spending

### Integration Example

```python
import httpx

async def get_kimchi_premium(symbol: str):
    async with httpx.AsyncClient() as client:
        # First request will return 402
        response = await client.get(
            f"https://api.printmoneylab.com/api/v1/kimchi-premium?symbol={symbol}"
        )
        
        if response.status_code == 402:
            # Extract x402 payment requirements
            x402_headers = response.headers
            # ... sign payment with your wallet ...
            # Retry with X-Payment header
            
        return response.json()
```

## Questions?

- Open an issue for bug reports or feature requests
- Check existing issues before creating new ones
- Be patient - this is a community-driven project

## License

By contributing, you agree that your contributions will be licensed under the same license as the project.

---

**Thank you for contributing to KR Crypto Intelligence API!** 🚀
