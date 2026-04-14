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


@mcp.tool()
async def get_arbitrage_scanner() -> dict:
    """Scan Kimchi Premium for ALL tokens (180+) traded on both Upbit and Binance.
    Returns: token-by-token premium %, reverse premiums (negative = Korean discount),
    Upbit vs Bithumb price gaps, market share between exchanges.
    Each token includes: warning flags, volume soaring alerts, deposit soaring alerts.
    Updated every 60 seconds. Essential for cross-exchange arbitrage analysis.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/arbitrage-scanner")
        return r.json()

@mcp.tool()
async def get_exchange_alerts() -> dict:
    """Get Korean exchange alerts: new listings, delistings, investment warnings, and caution flags.
    Detects: INVESTMENT_WARNING, PRICE_FLUCTUATIONS, VOLUME_SOARING, DEPOSIT_SOARING,
    GLOBAL_PRICE_DIFF, SMALL_ACCOUNTS_CONCENTRATION.
    New listings/delistings detected by comparing market list changes every 60 seconds.
    Critical for risk management and early listing detection.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/exchange-alerts")
        return r.json()

@mcp.tool()
async def get_market_movers() -> dict:
    """Get Korean market movers: 1-minute price surges/crashes (>1%), volume spikes,
    and top 20 tokens by trading volume on Upbit.
    Detects rapid price movements and unusual volume activity in Korean crypto markets.
    Korean retail activity often leads global price movements — early signal for traders.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/market-movers")
        return r.json()

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8443)

import anthropic
import asyncio
import os

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

@mcp.tool()
async def get_market_read() -> dict:
    """AI-powered Korean crypto market analysis. Combines Kimchi Premium, stablecoin premium, FX rate, Upbit/Bithumb volume rankings, Binance funding rate, open interest, BTC dominance, and Fear & Greed index. Returns AI-generated signal (BULLISH/BEARISH/NEUTRAL), confidence score, actionable summary, and all raw data. Price: $0.10 via x402."""
    import httpx, json

    async def _fetch(client, url, params=None):
        try:
            r = await client.get(url, params=params)
            return r.json() if r.status_code == 200 else None
        except:
            return None

    async with httpx.AsyncClient(timeout=12) as c:
        upbit_btc, binance_btc, fx_raw, upbit_all, bithumb_all, funding, oi, globe, fng = await asyncio.gather(
            _fetch(c, "https://api.upbit.com/v1/ticker", {"markets": "KRW-BTC"}),
            _fetch(c, "https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"}),
            _fetch(c, "https://open.er-api.com/v6/latest/USD"),
            _fetch(c, "https://api.upbit.com/v1/ticker/all?quote_currencies=KRW"),
            _fetch(c, "https://api.bithumb.com/public/ticker/ALL_KRW"),
            _fetch(c, "https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}),
            _fetch(c, "https://fapi.binance.com/fapi/v1/openInterest", {"symbol": "BTCUSDT"}),
            _fetch(c, "https://api.coingecko.com/api/v3/global"),
            _fetch(c, "https://api.alternative.me/fng/?limit=1"),
        )

    # 김치 프리미엄
    kimchi = None
    fx_rate = None
    if fx_raw and "rates" in fx_raw:
        fx_rate = fx_raw["rates"].get("KRW")
    if upbit_btc and binance_btc and fx_rate:
        u_krw = float(upbit_btc[0]["trade_price"])
        b_usd = float(binance_btc["price"])
        b_krw = b_usd * fx_rate
        prem = ((u_krw - b_krw) / b_krw) * 100
        kimchi = {"symbol": "BTC", "premium_pct": round(prem, 2), "upbit_krw": u_krw, "binance_usd": b_usd, "direction": "positive" if prem > 0 else "negative"}

    # 스테이블코인 프리미엄
    stable = None
    if upbit_all and fx_rate:
        sp = {}
        for t in upbit_all:
            if t["market"] in ["KRW-USDT", "KRW-USDC"]:
                name = t["market"].replace("KRW-", "").lower()
                krw_p = float(t["trade_price"])
                sp[name] = {"krw_price": krw_p, "premium_pct": round(((krw_p - fx_rate) / fx_rate) * 100, 2)}
        if sp:
            avg = sum(v["premium_pct"] for v in sp.values()) / len(sp)
            sp["direction"] = "inflow" if avg > 0 else "outflow"
            sp["avg_premium_pct"] = round(avg, 2)
            stable = sp

    # 업비트 거래량 TOP 5
    upbit_top = []
    if upbit_all:
        st = sorted(upbit_all, key=lambda x: float(x.get("acc_trade_price_24h", 0)), reverse=True)
        for t in st[:5]:
            upbit_top.append({"symbol": t["market"].replace("KRW-",""), "change_24h_pct": round(float(t.get("signed_change_rate",0))*100,2), "volume_krw_billion": round(float(t.get("acc_trade_price_24h",0))/1e9,1)})

    # 빗썸 거래량 TOP 5
    bithumb_top = []
    if bithumb_all and "data" in bithumb_all:
        tl = []
        for sym, info in bithumb_all["data"].items():
            if sym == "date" or not isinstance(info, dict):
                continue
            try:
                tl.append({"symbol": sym, "volume_krw_billion": round(float(info.get("acc_trade_value_24H",0))/1e9,1), "change_24h_pct": round(float(info.get("fluctate_rate_24H",0)),2)})
            except:
                continue
        bithumb_top = sorted(tl, key=lambda x: x["volume_krw_billion"], reverse=True)[:5]

    # 글로벌 지표
    fund = None
    if funding:
        rate = float(funding.get("lastFundingRate",0))*100
        fund = {"funding_rate_pct": round(rate,4), "mark_price": round(float(funding.get("markPrice",0)),2), "interpretation": "longs_pay" if rate>0 else "shorts_pay" if rate<0 else "neutral"}

    open_int = None
    if oi and funding:
        oi_btc = float(oi.get("openInterest",0))
        mark = float(funding.get("markPrice",0))
        open_int = {"open_interest_btc": round(oi_btc,2), "open_interest_usd_billion": round(oi_btc*mark/1e9,2)}

    dom = None
    if globe and "data" in globe:
        pct = globe["data"]["market_cap_percentage"]
        btc_d = pct.get("btc",0)
        eth_d = pct.get("eth",0)
        dom = {"btc_dominance_pct": round(btc_d,1), "eth_dominance_pct": round(eth_d,1), "alt_dominance_pct": round(100-btc_d-eth_d,1)}

    fear = None
    if fng and "data" in fng:
        d = fng["data"][0]
        fear = {"value": int(d["value"]), "label": d["value_classification"]}

    market_data = {
        "korean_market": {"kimchi_premium": kimchi, "stablecoin_premium": stable, "fx_rate": {"rate": fx_rate} if fx_rate else None, "upbit_volume_top5": upbit_top, "bithumb_volume_top5": bithumb_top},
        "global_market": {"btc_funding_rate": fund, "btc_open_interest": open_int, "dominance": dom, "fear_greed_index": fear},
    }

    # Claude AI
    def _call_claude():
        prompt = f"""You are a senior Korean crypto market analyst. Analyze this data:

{json.dumps(market_data, indent=2, ensure_ascii=False)}

Respond ONLY with JSON (no markdown):
{{"signal":"BULLISH or BEARISH or NEUTRAL","confidence":7,"summary":"2-3 sentences with actual numbers.","key_factors":["f1","f2","f3"],"risk_warning":"One sentence."}}"""
        try:
            cl = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = cl.messages.create(model="claude-haiku-4-5-20251001", max_tokens=500, messages=[{"role":"user","content":prompt}])
            txt = msg.content[0].text.strip()
            if txt.startswith("```"):
                txt = txt.split("```")[1]
                if txt.startswith("json"): txt = txt[4:]
                txt = txt.strip()
            return json.loads(txt)
        except Exception as e:
            return {"signal":"ERROR","confidence":0,"summary":str(e)[:100],"key_factors":[],"risk_warning":"Unavailable"}

    loop = asyncio.get_event_loop()
    ai = await loop.run_in_executor(None, _call_claude)

    return {
        "signal": ai.get("signal","NEUTRAL"),
        "confidence": f'{ai.get("confidence",0)}/10',
        "summary": ai.get("summary",""),
        "key_factors": ai.get("key_factors",[]),
        "risk_warning": ai.get("risk_warning",""),
        "data": market_data,
        "meta": {"price":"$0.10","data_sources":["upbit","bithumb","binance_futures","coingecko","alternative.me"],"ai_model":"claude-haiku-4.5"},
    }
