import httpx
from fastmcp import FastMCP

API_BASE = "http://127.0.0.1:80"

mcp = FastMCP("KR Crypto Intelligence")

@mcp.tool()
async def get_kimchi_premium(symbol: str = "BTC") -> dict:
    """Get real-time Kimchi Premium — the price difference between Korean exchanges (Upbit) and global exchanges (Binance).
    South Korea ranks top 3 globally in crypto trading volume.
    A positive premium means Korean traders are paying more than the global market price.

    Args:
        symbol: Crypto symbol (e.g., BTC, ETH, XRP, SOL, DOGE)
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/kimchi-premium", params={"symbol": symbol})
        return r.json()

@mcp.tool()
async def get_kr_prices(symbol: str = "BTC", exchange: str = "all") -> dict:
    """Get cryptocurrency prices from Korean exchanges (Upbit, Bithumb).
    Returns KRW-denominated prices, 24h volume, and change rate.

    Args:
        symbol: Crypto symbol (e.g., BTC, ETH, XRP, SOL, DOGE)
        exchange: Exchange to query — 'upbit', 'bithumb', or 'all' for both
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/kr-prices", params={"symbol": symbol, "exchange": exchange})
        return r.json()

@mcp.tool()
async def get_fx_rate() -> dict:
    """Get current USD/KRW exchange rate.
    Essential for converting between Korean Won and US Dollar prices.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/fx-rate")
        return r.json()

@mcp.tool()
async def get_available_symbols() -> dict:
    """Get all available trading symbols on Korean exchanges.
    Returns symbols available on Upbit, Bithumb, and those common to both.
    Use this to check which symbols you can query before calling other tools.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/symbols")
        return r.json()


@mcp.tool()
async def get_stablecoin_premium() -> dict:
    """Get USDT and USDC premium on Korean exchanges vs official USD/KRW rate.
    Positive premium = capital flowing INTO Korean crypto market.
    Negative premium = capital flowing OUT.
    This is a key indicator of Korean market fund flow direction, separate from Kimchi Premium.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/stablecoin-premium")
        return r.json()

@mcp.tool()
async def check_health() -> dict:
    """Check service health and exchange connectivity status.
    Returns status of Upbit, Bithumb, and Binance API connections.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/health")
        return r.json()

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8443)
