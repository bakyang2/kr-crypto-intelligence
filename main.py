import time
import json
import re
import os
import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse

# === x402 결제 ===
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
import anthropic
from patch_exchange_intel import intel_cache, compute_intel_data, intel_polling_task, load_alert_history
import asyncio
from cdp.x402 import create_facilitator_config
from x402.http.types import RouteConfig
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mechanisms.svm.exact import ExactSvmServerScheme

# === 설정 ===
CACHE_TTL = 15
SYMBOL_CACHE_TTL = 300
MAX_CACHE_SIZE = 100
RATE_LIMIT_PER_MINUTE = 60
STATS_FILE = "/home/ubuntu/KRCryptoAPI/stats.json"
STATS_SAVE_INTERVAL = 60
EXCHANGE_TIMEOUT = 10

# === 텔레그램 설정 ===
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
tg_pending = []
tg_last_summary = 0

async def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                         json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"})
    except Exception:
        pass

async def tg_notify_request(endpoint, symbol, ip, status_code=200):
    global tg_last_summary, tg_pending
    PAID_ENDPOINTS = ["/api/v1/kimchi-premium", "/api/v1/kr-prices", "/api/v1/fx-rate", "/api/v1/stablecoin-premium", "/api/v1/market-read", "/api/v1/arbitrage-scanner", "/api/v1/exchange-alerts", "/api/v1/market-movers"]
    if endpoint in PAID_ENDPOINTS and status_code < 400:
        price_map = {
            "/api/v1/market-read": "$0.10",
            "/api/v1/arbitrage-scanner": "$0.01",
            "/api/v1/exchange-alerts": "$0.01",
            "/api/v1/market-movers": "$0.01",
        }
        price = price_map.get(endpoint, "$0.001")
        await tg_send(f"💰 유료 결제 성공!\n엔드포인트: {endpoint}\n가격: {price}\nIP: {ip}\n시간: {time.strftime('%H:%M:%S')}")
    tg_pending.append({"endpoint": endpoint, "ip": ip, "time": time.strftime("%H:%M:%S"), "ok": status_code < 400})
    now = time.time()
    if now - tg_last_summary >= 60 and tg_pending:
        count = len(tg_pending)
        eps = {}
        for r in tg_pending:
            eps[r["endpoint"]] = eps.get(r["endpoint"], 0) + 1
        summary = "\n".join([f"  {k}: {v}" for k, v in eps.items()])
        ips = len(set(r["ip"] for r in tg_pending))
        ok_count = sum(1 for r in tg_pending if r.get("ok"))
        fail_count = count - ok_count
        status_line = f"✅ {ok_count}건 성공" + (f" | ❌ {fail_count}건 실패" if fail_count else "")
        await tg_send(f"📊 <b>최근 요청</b>\n{status_line} | IP {ips}개\n{summary}")
        tg_pending.clear()
        tg_last_summary = now

# === 전역 상태 ===
cache = {}
rate_limit_store = defaultdict(list)
start_time = time.time()
stats = {
    "total_requests": 0,
    "today_date": "",
    "today_requests": 0,
    "by_endpoint": defaultdict(int),
    "errors": 0,
    "last_request_at": None
}

# === 캐시 ===
def get_cache(key):
    if key in cache:
        data, timestamp = cache[key]
        age = time.time() - timestamp
        ttl = SYMBOL_CACHE_TTL if key == "symbols" else CACHE_TTL
        if age < ttl:
            return data, age
    return None, 0

def set_cache(key, data):
    if len(cache) > MAX_CACHE_SIZE:
        now = time.time()
        expired = [k for k, (_, ts) in cache.items() if now - ts > CACHE_TTL]
        for k in expired:
            del cache[k]
        if len(cache) > MAX_CACHE_SIZE:
            oldest = min(cache, key=lambda k: cache[k][1])
            del cache[oldest]
    cache[key] = (data, time.time())

# === Rate Limiter ===
def check_rate_limit(ip):
    now = time.time()
    rate_limit_store[ip] = [t for t in rate_limit_store[ip] if now - t < 60]
    if len(rate_limit_store[ip]) >= RATE_LIMIT_PER_MINUTE:
        return False
    rate_limit_store[ip].append(now)
    if len(rate_limit_store) > 1000:
        dead = [k for k, v in rate_limit_store.items() if not v or now - max(v) > 60]
        for k in dead:
            del rate_limit_store[k]
    return True

def get_real_ip(request):
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.client.host
    )

# === 통계 ===
def load_stats():
    global stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r") as f:
                saved = json.load(f)
                stats["total_requests"] = saved.get("total_requests", 0)
                stats["today_date"] = saved.get("today_date", "")
                stats["today_requests"] = saved.get("today_requests", 0)
                stats["by_endpoint"] = defaultdict(int, saved.get("by_endpoint", {}))
                stats["errors"] = saved.get("errors", 0)
                stats["last_request_at"] = saved.get("last_request_at")
    except Exception:
        pass

def save_stats():
    try:
        with open(STATS_FILE, "w") as f:
            json.dump({
                "total_requests": stats["total_requests"],
                "today_date": stats["today_date"],
                "today_requests": stats["today_requests"],
                "by_endpoint": dict(stats["by_endpoint"]),
                "errors": stats["errors"],
                "last_request_at": stats["last_request_at"]
            }, f)
    except Exception:
        pass

def track_request(endpoint):
    today = time.strftime("%Y-%m-%d")
    if stats["today_date"] != today:
        stats["today_date"] = today
        stats["today_requests"] = 0
    stats["total_requests"] += 1
    stats["today_requests"] += 1
    stats["by_endpoint"][endpoint] += 1
    stats["last_request_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

# === 심볼 유효성 검사 ===
SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,10}$")

def validate_symbol(symbol):
    symbol = symbol.upper().strip()
    if not SYMBOL_PATTERN.match(symbol):
        raise HTTPException(status_code=400, detail=f"Invalid symbol: '{symbol}'. Use 1-10 uppercase letters (e.g., BTC, ETH, XRP).")
    return symbol

# === 거래소 API ===
async def fetch_upbit_price(symbol):
    cached, age = get_cache(f"upbit_{symbol}")
    if cached:
        cached["data_age_seconds"] = round(age, 1)
        return cached
    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
        r = await client.get(f"https://api.upbit.com/v1/ticker?markets=KRW-{symbol}")
        if r.status_code == 404:
            return {"error": f"Symbol {symbol} not found on Upbit"}
        r.raise_for_status()
        data = r.json()
        if not data:
            return {"error": f"Symbol {symbol} not found on Upbit"}
        d = data[0]
        result = {
            "exchange": "upbit",
            "symbol": symbol,
            "price_krw": d["trade_price"],
            "volume_24h": d.get("acc_trade_volume_24h"),
            "change_rate": d.get("signed_change_rate"),
            "timestamp": d.get("trade_timestamp"),
            "data_age_seconds": 0
        }
        set_cache(f"upbit_{symbol}", result)
        return result

async def fetch_bithumb_price(symbol):
    cached, age = get_cache(f"bithumb_{symbol}")
    if cached:
        cached["data_age_seconds"] = round(age, 1)
        return cached
    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
        r = await client.get(f"https://api.bithumb.com/public/ticker/{symbol}_KRW")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "0000":
            msg = body.get("message", "Unknown error")
            if "not found" in msg.lower() or body.get("status") == "5300":
                return {"error": f"Symbol {symbol} not found on Bithumb"}
            if any(w in msg for w in ["점검", "maintenance", "Maintenance"]):
                return {"error": "Bithumb is under maintenance", "status": "exchange_maintenance"}
            return {"error": f"Bithumb API error: {msg}"}
        data = body["data"]
        result = {
            "exchange": "bithumb",
            "symbol": symbol,
            "price_krw": float(data["closing_price"]),
            "volume_24h": float(data["units_traded_24H"]),
            "change_rate": float(data["fluctate_rate_24H"]) / 100,
            "timestamp": int(data["date"]),
            "data_age_seconds": 0
        }
        set_cache(f"bithumb_{symbol}", result)
        return result

async def fetch_binance_price(symbol):
    cached, age = get_cache(f"binance_{symbol}")
    if cached:
        cached["data_age_seconds"] = round(age, 1)
        return cached
    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
        r = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT")
        if r.status_code == 400:
            return {"error": f"Symbol {symbol}USDT not found on Binance. This coin may only be listed on Korean exchanges."}
        r.raise_for_status()
        data = r.json()
        result = {
            "exchange": "binance",
            "symbol": symbol,
            "price_usdt": float(data["price"]),
            "data_age_seconds": 0
        }
        set_cache(f"binance_{symbol}", result)
        return result

async def fetch_fx_rate():
    cached, age = get_cache("fx_usd_krw")
    if cached:
        cached["data_age_seconds"] = round(age, 1)
        return cached
    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
        try:
            r = await client.get("https://api.exchangerate-api.com/v4/latest/USD")
            r.raise_for_status()
            rate = r.json()["rates"]["KRW"]
            source = "exchangerate-api.com"
        except Exception:
            try:
                upbit = await fetch_upbit_price("BTC")
                binance = await fetch_binance_price("BTC")
                if "error" in upbit or "error" in binance:
                    raise Exception("Fallback failed")
                rate = upbit["price_krw"] / binance["price_usdt"]
                source = "estimated_from_crypto"
            except Exception:
                raise HTTPException(status_code=503, detail="FX rate unavailable. Both primary and fallback sources failed.")
    result = {
        "base": "USD",
        "quote": "KRW",
        "rate": round(rate, 2),
        "source": source,
        "timestamp": int(time.time() * 1000),
        "data_age_seconds": 0
    }
    set_cache("fx_usd_krw", result)
    return result

async def fetch_available_symbols():
    cached, _ = get_cache("symbols")
    if cached:
        return cached
    symbols = {"upbit": [], "bithumb": []}
    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
        try:
            r = await client.get("https://api.upbit.com/v1/market/all")
            r.raise_for_status()
            for m in r.json():
                if m["market"].startswith("KRW-"):
                    symbols["upbit"].append(m["market"].replace("KRW-", ""))
        except Exception:
            pass
        try:
            r = await client.get("https://api.bithumb.com/public/ticker/ALL_KRW")
            r.raise_for_status()
            body = r.json()
            if body.get("status") == "0000":
                symbols["bithumb"] = [k for k in body.get("data", {}).keys() if k != "date"]
        except Exception:
            pass
    symbols["common"] = sorted(list(set(symbols["upbit"]) & set(symbols["bithumb"])))
    symbols["upbit"] = sorted(symbols["upbit"])
    symbols["bithumb"] = sorted(symbols["bithumb"])
    set_cache("symbols", symbols)
    return symbols

async def check_exchange_health():
    results = {}
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get("https://api.upbit.com/v1/ticker?markets=KRW-BTC")
            results["upbit"] = "ok" if r.status_code == 200 else f"error_{r.status_code}"
        except Exception as e:
            results["upbit"] = f"down: {type(e).__name__}"
        try:
            r = await client.get("https://api.bithumb.com/public/ticker/BTC_KRW")
            body = r.json()
            results["bithumb"] = "ok" if body.get("status") == "0000" else f"error: {body.get('message', 'unknown')}"
        except Exception as e:
            results["bithumb"] = f"down: {type(e).__name__}"
        try:
            r = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            results["binance"] = "ok" if r.status_code == 200 else f"error_{r.status_code}"
        except Exception as e:
            results["binance"] = f"down: {type(e).__name__}"
    return results

# === Background tasks ===
async def periodic_stats_save():
    while True:
        await asyncio.sleep(STATS_SAVE_INTERVAL)
        save_stats()

async def daily_summary_task():
    while True:
        now = time.localtime()
        seconds_until_midnight = (23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (59 - now.tm_sec)
        await asyncio.sleep(seconds_until_midnight + 1)
        await tg_send(
            f"📈 <b>일일 요약</b> ({stats.get('today_date', '')})\n"
            f"오늘 요청: {stats.get('today_requests', 0)}건\n"
            f"누적 요청: {stats.get('total_requests', 0)}건\n"
            f"에러: {stats.get('errors', 0)}건"
        )

# === FastAPI 앱 ===
@asynccontextmanager
async def lifespan(app):
    load_stats()
    await tg_send("🟢 <b>KR Crypto API</b> 서버 시작됨\nhttps://api.printmoneylab.com/health")
    task1 = asyncio.create_task(periodic_stats_save())
    asyncio.create_task(intel_polling_task(fetch_fx_rate))
    task2 = asyncio.create_task(daily_summary_task())
    yield
    task1.cancel()
    task2.cancel()
    save_stats()

# === x402 결제 설정 ===
WALLET_ADDRESS = "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"
SOLANA_WALLET = "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" 
FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

cdp_config = create_facilitator_config()
x402_server = x402ResourceServer(
    HTTPFacilitatorClient(cdp_config)
)
x402_server.register("eip155:8453", ExactEvmServerScheme())
x402_server.register("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", ExactSvmServerScheme())

x402_routes = {
    "GET /api/v1/kimchi-premium": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.001", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.001", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/kr-prices": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.001", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.001", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/fx-rate": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.001", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.001", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/stablecoin-premium": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.001", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.001", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/arbitrage-scanner": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.01", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.01", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/exchange-alerts": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.01", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.01", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/market-movers": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.01", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.01", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
    "GET /api/v1/market-read": RouteConfig(
        accepts=[
            PaymentOption(scheme="exact", price="$0.1", network="eip155:8453", pay_to=WALLET_ADDRESS),
            PaymentOption(scheme="exact", price="$0.1", network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
        ]
    ),
}

app = FastAPI(
    title="KR Crypto Intelligence API",
    description="Korean crypto market data for AI agents. Kimchi premium, exchange prices, FX rates.",
    version="0.1.0",
    lifespan=lifespan
)

# x402 결제 미들웨어 적용
app.add_middleware(PaymentMiddlewareASGI, routes=x402_routes, server=x402_server)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/docs", "/openapi.json", "/", "/favicon.ico"):
        return await call_next(request)
    ip = get_real_ip(request)
    if not check_rate_limit(ip):
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Max 60 requests per minute.", "retry_after_seconds": 60})
    response = await call_next(request)
    try:
        endpoint = request.url.path
        if endpoint.startswith("/api/v1/"):
            symbol = request.query_params.get("symbol", "")
            await tg_notify_request(endpoint, symbol, ip, response.status_code)
    except Exception:
        pass
    return response

# === 엔드포인트 ===
@app.get("/")
async def root():
    return {
        "service": "KR Crypto Intelligence API",
        "version": "0.1.0",
        "description": "Korean crypto market data for AI agents",
        "endpoints": {
            "/api/v1/kimchi-premium": "Real-time Kimchi Premium (Upbit vs Binance)",
            "/api/v1/kr-prices": "Korean exchange prices (Upbit, Bithumb)",
            "/api/v1/fx-rate": "USD/KRW exchange rate",
            "/api/v1/symbols": "Available trading symbols",
            "/api/v1/stats": "API usage statistics",
            "/health": "Service health check (free)"
        }
    }

@app.get("/.well-known/x402")
async def x402_manifest():
    """x402 service discovery manifest."""
    return {
        "x402Version": 2,
        "name": "KR Crypto Intelligence",
        "description": "Korean crypto market data + AI analysis for AI agents. 10 endpoints, 180+ tokens. Kimchi Premium, exchange intelligence, AI market read.",
        "url": "https://api.printmoneylab.com",
        "mcp": "https://mcp.printmoneylab.com/mcp",
        "source": "https://github.com/bakyang2/kr-crypto-intelligence",
        "endpoints": [
            {"path": "/api/v1/kimchi-premium", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Real-time Kimchi Premium (Upbit vs Binance)"},
            {"path": "/api/v1/kr-prices", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean exchange prices (Upbit, Bithumb)"},
            {"path": "/api/v1/fx-rate", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "USD/KRW exchange rate"},
            {"path": "/api/v1/stablecoin-premium", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "USDT/USDC premium on Korean exchanges (fund flow indicator)"},
            {"path": "/api/v1/arbitrage-scanner", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Token-by-token Kimchi Premium for 180+ tokens, reverse premium, Upbit-Bithumb gaps, market share"},
            {"path": "/api/v1/exchange-alerts", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "New listings/delistings, investment warnings, caution flags"},
            {"path": "/api/v1/market-movers", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "1-min price surges/crashes, volume spikes, top volume tokens"},
            {"path": "/api/v1/market-read", "method": "GET", "price": "$0.10", "networks": ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "AI market analysis — 12+ data sources + exchange intelligence + Claude AI token-level signals"}
        ],
        "free_endpoints": [
            {"path": "/api/v1/symbols", "method": "GET", "description": "Available trading symbols"},
            {"path": "/health", "method": "GET", "description": "Service health check"},
            {"path": "/api/v1/stats", "method": "GET", "description": "API usage statistics"}
        ],
        "payment": [
            {"scheme": "exact", "network": "eip155:8453", "asset": "USDC", "payTo": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"},
            {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "asset": "USDC", "payTo": "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9"}
        ],
        "tags": ["korean", "crypto", "kimchi-premium", "upbit", "bithumb", "fx-rate", "market-data", "asia", "arbitrage", "exchange-intelligence", "ai-analysis"]
    }

@app.get("/health")
async def health():
    exchanges = await check_exchange_health()
    all_ok = all(v == "ok" for v in exchanges.values())
    any_ok = any(v == "ok" for v in exchanges.values())
    return {
        "status": "ok" if all_ok else ("degraded" if any_ok else "down"),
        "exchanges": exchanges,
        "cache_size": len(cache),
        "uptime_seconds": round(time.time() - start_time),
        "timestamp": int(time.time() * 1000)
    }

@app.get("/api/v1/symbols")
async def symbols():
    track_request("symbols")
    result = await fetch_available_symbols()
    return {
        "upbit_count": len(result["upbit"]),
        "bithumb_count": len(result["bithumb"]),
        "common_count": len(result["common"]),
        "common": result["common"],
        "upbit_only": sorted(list(set(result["upbit"]) - set(result["bithumb"]))),
        "bithumb_only": sorted(list(set(result["bithumb"]) - set(result["upbit"]))),
        "timestamp": int(time.time() * 1000)
    }

@app.get("/api/v1/kimchi-premium")
async def kimchi_premium(symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)")):
    track_request("kimchi-premium")
    symbol = validate_symbol(symbol)
    try:
        upbit = await fetch_upbit_price(symbol)
        if "error" in upbit:
            raise HTTPException(status_code=404, detail=upbit["error"])
        binance = await fetch_binance_price(symbol)
        if "error" in binance:
            raise HTTPException(status_code=404, detail={"message": binance["error"], "suggestion": f"Use /api/v1/kr-prices?symbol={symbol} for Korean-only price data."})
        fx = await fetch_fx_rate()
        binance_krw = binance["price_usdt"] * fx["rate"]
        premium_pct = ((upbit["price_krw"] - binance_krw) / binance_krw) * 100
        result = {
            "symbol": symbol,
            "upbit_krw": upbit["price_krw"],
            "binance_usdt": binance["price_usdt"],
            "fx_rate": fx["rate"],
            "fx_source": fx["source"],
            "binance_krw_equivalent": round(binance_krw, 0),
            "premium_percent": round(premium_pct, 2),
            "premium_direction": "positive" if premium_pct > 0 else "negative",
            "timestamp": int(time.time() * 1000)
        }
        if fx["source"] == "estimated_from_crypto":
            result["warning"] = "FX rate estimated from crypto prices. Premium calculation may be less accurate."
        return result
    except HTTPException:
        stats["errors"] += 1
        raise
    except Exception as e:
        stats["errors"] += 1
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/stablecoin-premium")
async def stablecoin_premium():
    track_request("stablecoin-premium")
    try:
        fx = await fetch_fx_rate()
        official_rate = fx["rate"]
        results = {}
        for coin in ["USDT", "USDC"]:
            try:
                upbit = await fetch_upbit_price(coin)
                if "error" in upbit:
                    results[coin.lower()] = {"error": upbit["error"]}
                    continue
                price_krw = upbit["price_krw"]
                premium_pct = ((price_krw - official_rate) / official_rate) * 100
                results[coin.lower()] = {
                    "price_krw": price_krw,
                    "premium_percent": round(premium_pct, 2),
                    "premium_direction": "positive" if premium_pct > 0 else "negative",
                    "volume_24h": upbit.get("volume_24h"),
                }
            except Exception as e:
                results[coin.lower()] = {"error": str(e)}
        return {
            "official_fx_rate": official_rate,
            "fx_source": fx["source"],
            "stablecoins": results,
            "interpretation": {
                "positive_premium": "Capital flowing INTO Korean crypto market",
                "negative_premium": "Capital flowing OUT of Korean crypto market",
            },
            "timestamp": int(time.time() * 1000),
        }
    except HTTPException:
        stats["errors"] += 1
        raise
    except Exception as e:
        stats["errors"] += 1
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/kr-prices")
async def kr_prices(
    symbol: str = Query(default="BTC", description="Crypto symbol"),
    exchange: str = Query(default="all", description="Exchange: upbit, bithumb, or all")
):
    track_request("kr-prices")
    symbol = validate_symbol(symbol)
    exchange = exchange.lower().strip()
    if exchange not in ("upbit", "bithumb", "all"):
        raise HTTPException(status_code=400, detail=f"Unknown exchange: '{exchange}'. Use 'upbit', 'bithumb', or 'all'.")
    results = {}
    if exchange in ("upbit", "all"):
        try:
            results["upbit"] = await fetch_upbit_price(symbol)
        except Exception as e:
            results["upbit"] = {"error": f"Upbit request failed: {type(e).__name__}"}
    if exchange in ("bithumb", "all"):
        try:
            results["bithumb"] = await fetch_bithumb_price(symbol)
        except Exception as e:
            results["bithumb"] = {"error": f"Bithumb request failed: {type(e).__name__}"}
    if all("error" in v for v in results.values()):
        stats["errors"] += 1
    return {"symbol": symbol, "data": results, "timestamp": int(time.time() * 1000)}

@app.get("/api/v1/fx-rate")
async def fx_rate_endpoint():
    track_request("fx-rate")
    try:
        return await fetch_fx_rate()
    except HTTPException:
        stats["errors"] += 1
        raise
    except Exception as e:
        stats["errors"] += 1
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/stats")
async def get_stats():
    return {
        "total_requests": stats["total_requests"],
        "today_date": stats["today_date"],
        "today_requests": stats["today_requests"],
        "by_endpoint": dict(stats["by_endpoint"]),
        "errors": stats["errors"],
        "last_request_at": stats["last_request_at"],
        "uptime_seconds": round(time.time() - start_time),
        "cache_size": len(cache)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


# ============================================================
# Market Read - AI-powered Korean crypto market analysis
# ============================================================

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

async def fetch_upbit_volume_top(limit=5):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.upbit.com/v1/ticker/all?quote_currencies=KRW")
            if r.status_code != 200:
                return []
            tickers = r.json()
            sorted_t = sorted(tickers, key=lambda x: float(x.get("acc_trade_price_24h", 0)), reverse=True)
            result = []
            for t in sorted_t[:limit]:
                sym = t["market"].replace("KRW-", "")
                change_rate = float(t.get("signed_change_rate", 0)) * 100
                volume_krw = float(t.get("acc_trade_price_24h", 0))
                result.append({
                    "symbol": sym,
                    "change_24h_pct": round(change_rate, 2),
                    "volume_krw_billion": round(volume_krw / 1e9, 1),
                    "price_krw": float(t.get("trade_price", 0)),
                })
            return result
    except Exception as e:
        print(f"[WARN] upbit volume top: {e}")
        return []

async def fetch_bithumb_volume_top(limit=5):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.bithumb.com/public/ticker/ALL_KRW")
            if r.status_code != 200:
                return []
            data = r.json().get("data", {})
            tickers = []
            for sym, info in data.items():
                if sym == "date" or not isinstance(info, dict):
                    continue
                try:
                    vol = float(info.get("acc_trade_value_24H", 0))
                    price = float(info.get("closing_price", 0))
                    change = float(info.get("fluctate_rate_24H", 0))
                    tickers.append({
                        "symbol": sym,
                        "volume_krw_billion": round(vol / 1e9, 1),
                        "price_krw": price,
                        "change_24h_pct": round(change, 2),
                    })
                except (ValueError, TypeError):
                    continue
            sorted_t = sorted(tickers, key=lambda x: x["volume_krw_billion"], reverse=True)
            return sorted_t[:limit]
    except Exception as e:
        print(f"[WARN] bithumb volume top: {e}")
        return []

async def fetch_binance_funding_rate():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"})
            if r.status_code != 200:
                return None
            data = r.json()
            rate = float(data.get("lastFundingRate", 0)) * 100
            return {
                "funding_rate_pct": round(rate, 4),
                "mark_price": round(float(data.get("markPrice", 0)), 2),
                "interpretation": "longs_pay" if rate > 0 else "shorts_pay" if rate < 0 else "neutral",
            }
    except Exception as e:
        print(f"[WARN] funding rate: {e}")
        return None

async def fetch_binance_open_interest():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": "BTCUSDT"})
            if r.status_code != 200:
                return None
            oi_btc = float(r.json().get("openInterest", 0))
            r2 = await client.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"})
            mark = float(r2.json().get("markPrice", 0)) if r2.status_code == 200 else 0
            oi_usd = oi_btc * mark
            return {
                "open_interest_btc": round(oi_btc, 2),
                "open_interest_usd_billion": round(oi_usd / 1e9, 2),
            }
    except Exception as e:
        print(f"[WARN] open interest: {e}")
        return None

async def fetch_btc_dominance():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.coingecko.com/api/v3/global")
            if r.status_code != 200:
                return None
            pct = r.json()["data"]["market_cap_percentage"]
            btc = pct.get("btc", 0)
            eth = pct.get("eth", 0)
            return {
                "btc_dominance_pct": round(btc, 1),
                "eth_dominance_pct": round(eth, 1),
                "alt_dominance_pct": round(100 - btc - eth, 1),
            }
    except Exception as e:
        print(f"[WARN] dominance: {e}")
        return None

async def fetch_fear_greed():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
            if r.status_code != 200:
                return None
            data = r.json()["data"][0]
            return {
                "value": int(data["value"]),
                "label": data["value_classification"],
            }
    except Exception as e:
        print(f"[WARN] fear greed: {e}")
        return None

async def kimchi_premium_data(symbol="BTC"):
    try:
        upbit = await fetch_upbit_price(symbol)
        binance = await fetch_binance_price(symbol)
        fx = await fetch_fx_rate()
        if not all([upbit, binance, fx]):
            return None
        upbit_krw = upbit["price_krw"]
        binance_krw = binance["price_usdt"] * fx["rate"]
        premium = ((upbit_krw - binance_krw) / binance_krw) * 100
        return {
            "symbol": symbol,
            "premium_pct": round(premium, 2),
            "upbit_krw": upbit_krw,
            "binance_usd": binance["price_usdt"],
            "direction": "positive" if premium > 0 else "negative",
        }
    except Exception as e:
        print(f"[WARN] kimchi premium data: {e}")
        return None

async def stablecoin_premium_data():
    try:
        fx = await fetch_fx_rate()
        if not fx:
            return None
        official_rate = fx["rate"]
        result = {}
        async with httpx.AsyncClient(timeout=10) as client:
            for stable in ["USDT", "USDC"]:
                r = await client.get(f"https://api.upbit.com/v1/ticker?markets=KRW-{stable}")
                if r.status_code == 200:
                    data = r.json()
                    if data:
                        krw_price = float(data[0]["trade_price"])
                        premium = ((krw_price - official_rate) / official_rate) * 100
                        result[stable.lower()] = {
                            "krw_price": krw_price,
                            "premium_pct": round(premium, 2),
                        }
        if result:
            avg = sum(v["premium_pct"] for v in result.values()) / len(result)
            result["direction"] = "inflow" if avg > 0 else "outflow"
            result["avg_premium_pct"] = round(avg, 2)
        return result if result else None
    except Exception as e:
        print(f"[WARN] stablecoin premium data: {e}")
        return None

def call_claude_sync(market_data):
    prompt = f"""You are a senior Korean crypto market analyst providing actionable intelligence to AI trading agents.

Analyze this real-time data and provide a structured market read:

{json.dumps(market_data, indent=2, ensure_ascii=False)}

Rules:
- Be specific. Reference actual numbers from the data.
- The summary should be 3-4 sentences of actionable insight.
- Include TOKEN-LEVEL calls when exchange_intelligence data shows notable signals (caution flags, premium outliers, volume spikes, 1-min movers).
- Confidence is 1-10 based on how aligned the signals are.
- key_factors should list the 4-5 most important data points driving your signal.
- token_alerts should list specific tokens with actionable flags (e.g. "WET: volume + deposit soaring = overheated, avoid longs").

Respond ONLY with this JSON (no markdown, no backticks):
{{"signal":"BULLISH or BEARISH or NEUTRAL","confidence":7,"summary":"Your analysis here.","key_factors":["factor1","factor2","factor3","factor4"],"token_alerts":["TOKEN1: reason","TOKEN2: reason"],"risk_warning":"Main risk to watch."}}"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[ERR] Claude JSON parse: {e}")
        return {"signal": "NEUTRAL", "confidence": 0, "summary": "AI parsing error. Raw data included.", "key_factors": [], "risk_warning": "Interpret raw data manually."}
    except Exception as e:
        print(f"[ERR] Claude API: {e}")
        return {"signal": "ERROR", "confidence": 0, "summary": f"AI error: {str(e)[:100]}", "key_factors": [], "risk_warning": "Service temporarily unavailable."}



# ============================================================
# Korean Exchange Intelligence Endpoints
# ============================================================

@app.get("/api/v1/arbitrage-scanner")
async def arbitrage_scanner(request: Request):
    """Token-by-token Kimchi Premium, reverse premium, Upbit-Bithumb gap, market share"""
    track_request("/api/v1/arbitrage-scanner")
    ip = get_real_ip(request)
    data = compute_intel_data()
    if not data:
        await tg_notify_request("/api/v1/arbitrage-scanner", "", ip, 503)
        raise HTTPException(status_code=503, detail="Intel data not ready yet. Try again in 60 seconds.")
    await tg_notify_request("/api/v1/arbitrage-scanner", "", ip)
    return {
        "premiums": data["premiums"],
        "reverse_premiums": [p for p in data["premiums"] if p["premium_pct"] < 0],
        "exchange_gaps": data["exchange_gaps"],
        "market_share": data["market_share"],
        "common_symbols_count": data["common_symbols_count"],
        "fx_rate": data["fx_rate"],
        "last_update": data["last_update"],
        "meta": {"price": "$0.01", "update_interval": "60s"},
    }

@app.get("/api/v1/exchange-alerts")
async def exchange_alerts(request: Request):
    """Listing changes, caution/warning tokens"""
    track_request("/api/v1/exchange-alerts")
    ip = get_real_ip(request)
    data = compute_intel_data()
    if not data:
        await tg_notify_request("/api/v1/exchange-alerts", "", ip, 503)
        raise HTTPException(status_code=503, detail="Intel data not ready yet. Try again in 60 seconds.")
    await tg_notify_request("/api/v1/exchange-alerts", "", ip)
    return {
        "listing_changes": data["listing_changes"],
        "caution_tokens": data["caution_tokens"],
        "last_update": data["last_update"],
        "meta": {"price": "$0.01", "update_interval": "60s"},
    }

@app.get("/api/v1/market-movers")
async def market_movers(request: Request):
    """Volume spikes, price surges/crashes, top volume tokens"""
    track_request("/api/v1/market-movers")
    ip = get_real_ip(request)
    data = compute_intel_data()
    if not data:
        await tg_notify_request("/api/v1/market-movers", "", ip, 503)
        raise HTTPException(status_code=503, detail="Intel data not ready yet. Try again in 60 seconds.")
    await tg_notify_request("/api/v1/market-movers", "", ip)
    return {
        "movers_1m": data["movers_1m"],
        "volume_spikes": data["vol_spikes"],
        "top_volume": data["top_volume"],
        "last_update": data["last_update"],
        "meta": {"price": "$0.01", "update_interval": "60s"},
    }

@app.get("/api/v1/market-read")
async def market_read(request: Request):
    """AI-powered Korean crypto market analysis."""
    import time as _time
    start = _time.time()
    ip = get_real_ip(request)

    try:
        results = await asyncio.gather(
            kimchi_premium_data("BTC"),
            stablecoin_premium_data(),
            fetch_fx_rate(),
            fetch_upbit_volume_top(5),
            fetch_bithumb_volume_top(5),
            fetch_binance_funding_rate(),
            fetch_binance_open_interest(),
            fetch_btc_dominance(),
            fetch_fear_greed(),
            return_exceptions=True,
        )

        def safe(r):
            return r if not isinstance(r, Exception) else None

        # intel 데이터 추가
        intel = compute_intel_data()
        intel_summary = {}
        if intel:
            top_premium = intel["premiums"][:5] if intel["premiums"] else []
            top_reverse = [p for p in intel["premiums"] if p["premium_pct"] < 0][:5]
            intel_summary = {
                "top_premium_tokens": [{"symbol": p["symbol"], "premium_pct": p["premium_pct"]} for p in top_premium],
                "top_reverse_premium": [{"symbol": p["symbol"], "premium_pct": p["premium_pct"]} for p in top_reverse],
                "caution_tokens": intel["caution_tokens"][:10],
                "movers_1m": intel["movers_1m"][:5],
                "volume_spikes": intel["vol_spikes"][:5],
                "exchange_gaps_top": intel["exchange_gaps"][:5],
                "market_share": intel["market_share"],
            }

        market_data = {
            "korean_market": {
                "kimchi_premium": safe(results[0]),
                "stablecoin_premium": safe(results[1]),
                "fx_rate": safe(results[2]),
                "upbit_volume_top5": safe(results[3]) or [],
                "bithumb_volume_top5": safe(results[4]) or [],
            },
            "global_market": {
                "btc_funding_rate": safe(results[5]),
                "btc_open_interest": safe(results[6]),
                "dominance": safe(results[7]),
                "fear_greed_index": safe(results[8]),
            },
            "exchange_intelligence": intel_summary,
        }

        loop = asyncio.get_event_loop()
        ai_analysis = await loop.run_in_executor(None, call_claude_sync, market_data)

        elapsed = round(_time.time() - start, 2)

        response = {
            "signal": ai_analysis.get("signal", "NEUTRAL"),
            "confidence": f'{ai_analysis.get("confidence", 0)}/10',
            "summary": ai_analysis.get("summary", ""),
            "key_factors": ai_analysis.get("key_factors", []),
            "token_alerts": ai_analysis.get("token_alerts", []),
            "risk_warning": ai_analysis.get("risk_warning", ""),
            "data": market_data,
            "meta": {
                "price": "$0.10",
                "processing_time_sec": elapsed,
                "data_sources": ["upbit", "bithumb", "binance_futures", "coingecko", "alternative.me", "exchange_intelligence(180+tokens)"],
                "ai_model": "claude-haiku-4.5",
            },
            "timestamp": int(_time.time() * 1000),
        }

        await tg_notify_request("/api/v1/market-read", "ALL", ip, 200)
        return response

    except Exception as e:
        print(f"[ERR] market-read: {e}")
        import traceback
        traceback.print_exc()
        await tg_notify_request("/api/v1/market-read", "ALL", ip, 500)
        return JSONResponse(status_code=500, content={"error": str(e)})
