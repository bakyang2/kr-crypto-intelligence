import time
import json
import re
import os
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

# === x402 결제 ===
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
import anthropic
from patch_exchange_intel import intel_cache, compute_intel_data, intel_polling_task, load_alert_history, tg_bot_polling
from cdp.x402 import create_facilitator_config
from x402.http.types import RouteConfig
from x402.server import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    declare_discovery_extension,
    OutputConfig,
)
from x402.mechanisms.svm.exact import ExactSvmServerScheme

from stats_logger import log_event, aggregate_stats, aggregate_stats_range, maybe_archive
from kr_sentiment import handle_kr_sentiment, load_cache_from_disk as load_sentiment_cache

# === 설정 ===
CACHE_TTL = 15
SYMBOL_CACHE_TTL = 300
MAX_CACHE_SIZE = 100
RATE_LIMIT_PER_MINUTE = 60
STATS_FILE = os.getenv("STATS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json"))
STATS_SAVE_INTERVAL = 60
EXCHANGE_TIMEOUT = 10

# === 텔레그램 설정 ===
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
# 실시간 알림 마스터 스위치: 기본적으로 결제 성공만 전송.
# true 로 설정 시 주기 집계/시작/이상감지 등 비결제 알림도 다시 활성화됨.
ENABLE_REALTIME_NON_PAYMENT_ALERTS = os.getenv("ENABLE_REALTIME_NON_PAYMENT_ALERTS", "false").lower() == "true"

# 결제 성공 알림 dedupe (엔드포인트+IP+분 단위). 5분 TTL.
_payment_alert_cache = {}

async def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                         json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"})
    except Exception:
        pass

async def tg_send_non_payment(text):
    """비결제 이벤트용 텔레그램 전송. ENABLE_REALTIME_NON_PAYMENT_ALERTS=false 시 no-op."""
    if not ENABLE_REALTIME_NON_PAYMENT_ALERTS:
        return
    await tg_send(text)

async def tg_notify_request(endpoint, symbol, ip, status_code=200):
    """요청당 1회만 호출되어야 함. 결제 성공만 즉시 알림. 비결제 집계는 플래그로만."""
    PAID_ENDPOINTS = ["/api/v1/kimchi-premium", "/api/v1/kr-prices", "/api/v1/fx-rate", "/api/v1/stablecoin-premium", "/api/v1/market-read", "/api/v1/arbitrage-scanner", "/api/v1/exchange-alerts", "/api/v1/market-movers", "/api/v1/kr-sentiment", "/api/v1/global-vs-korea-divergence", "/api/v1/global-vs-korea-divergence-deep"]
    if endpoint in PAID_ENDPOINTS and status_code == 200:
        # 5분 dedupe (같은 IP+endpoint 1회만)
        now = time.time()
        # 만료된 엔트리 청소
        expired = [k for k, ts in _payment_alert_cache.items() if now - ts > 300]
        for k in expired:
            del _payment_alert_cache[k]
        dedupe_key = f"{endpoint}:{ip}"
        if dedupe_key in _payment_alert_cache:
            return  # 5분 내 중복 억제
        _payment_alert_cache[dedupe_key] = now

        price_map = {
            "/api/v1/market-read": "$0.10",
            "/api/v1/kr-sentiment": "$0.05",
            "/api/v1/arbitrage-scanner": "$0.01",
            "/api/v1/exchange-alerts": "$0.01",
            "/api/v1/market-movers": "$0.01",
            "/api/v1/global-vs-korea-divergence": "$0.05",
            "/api/v1/global-vs-korea-divergence-deep": "$0.10",
        }
        price = price_map.get(endpoint, "$0.001")
        await tg_send(f"💰 유료 결제 성공!\n엔드포인트: {endpoint}\n가격: {price}\nIP: {ip}\n시간: {time.strftime('%H:%M:%S')}")
    # 주기 집계 알림(📊 최근 요청)은 ENABLE_REALTIME_NON_PAYMENT_ALERTS=false 일 때 비활성화
    # 집계 자체는 stats_logger로 계속 수집되어 일일 요약에서 사용됨

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
    """Legacy daily summary (simple stats). daily_report_task 와 중복되므로 비활성화됨.
    복구 시 ENABLE_REALTIME_NON_PAYMENT_ALERTS=true 로 설정."""
    while True:
        now = time.localtime()
        seconds_until_midnight = (23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (59 - now.tm_sec)
        await asyncio.sleep(seconds_until_midnight + 1)
        if not ENABLE_REALTIME_NON_PAYMENT_ALERTS:
            continue  # daily_report_task 가 09:00 KST에 상세 리포트 발송함
        await tg_send(
            f"📈 <b>일일 요약</b> ({stats.get('today_date', '')})\n"
            f"오늘 요청: {stats.get('today_requests', 0)}건\n"
            f"누적 요청: {stats.get('total_requests', 0)}건\n"
            f"에러: {stats.get('errors', 0)}건"
        )

async def daily_report_task():
    """매일 KST 09:00 (UTC 00:00)에 전일(KST 00:00~24:00) 상세 리포트 전송.
    이것은 결제 이외 알림 중 유일하게 살아있는 것 — 플래그 영향 받지 않음."""
    while True:
        try:
            now_kst = datetime.now(timezone(timedelta(hours=9)))
            # 다음 KST 09:00 계산 (현재가 09:00 전이면 오늘 09:00, 아니면 내일 09:00)
            next_9am = now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
            if now_kst >= next_9am:
                next_9am += timedelta(days=1)
            wait_seconds = (next_9am - now_kst).total_seconds()
            await asyncio.sleep(wait_seconds)

            # 전일 KST 00:00~24:00 구간 집계
            yesterday_midnight_kst = (next_9am.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
            today_midnight_kst = yesterday_midnight_kst + timedelta(days=1)
            yesterday_start = int(yesterday_midnight_kst.timestamp())
            yesterday_end = int(today_midnight_kst.timestamp())
            s = aggregate_stats_range(yesterday_start, yesterday_end)
            date_str = yesterday_midnight_kst.strftime("%Y-%m-%d")

            profit = s["revenue_usd"] - s["claude_cost_usd"]
            msg = (
                f"📊 <b>일일 리포트</b> — {date_str}\n\n"
                f"API 호출: {s['api_calls_total']}건 (HIT {s['cache_hits']}, MISS {s['api_calls_total'] - s['cache_hits']})\n"
                f"유료 결제: {s['paid_calls']}건 (${s['revenue_usd']:.2f})\n"
                f"김프 알림: {s['alerts_sent']}건\n"
                f"Claude 비용: ${s['claude_cost_usd']:.4f}\n"
                f"에러: {s['errors']}건\n\n"
                f"💰 일 순이익: ${profit:.4f}\n"
                f"{'─' * 25}"
            )
            await tg_send(msg)

            # Archive old stats if needed
            maybe_archive()
        except Exception as e:
            print(f"[DAILY] report error: {e}")

# === FastAPI 앱 ===
@asynccontextmanager
async def lifespan(app):
    load_stats()
    load_sentiment_cache()
    # 서버 시작 알림은 비결제 이벤트 — 플래그로 게이트
    await tg_send_non_payment("🟢 <b>KR Crypto API</b> 서버 시작됨\nhttps://api.printmoneylab.com/health")
    task1 = asyncio.create_task(periodic_stats_save())
    asyncio.create_task(intel_polling_task(fetch_fx_rate, tg_func=tg_send))
    asyncio.create_task(tg_bot_polling(TG_TOKEN, TG_CHAT))
    task2 = asyncio.create_task(daily_summary_task())
    task3 = asyncio.create_task(daily_report_task())
    # Coinness 뉴스 캐시 5분 주기 갱신 — divergence deep 응답 시간 단축용
    task4 = asyncio.create_task(coinness_news_poller())
    yield
    task1.cancel()
    task2.cancel()
    task3.cancel()
    task4.cancel()
    save_stats()

# === x402 결제 설정 ===
WALLET_ADDRESS = "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"
SOLANA_WALLET = "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
POLYGON_NETWORK = "eip155:137"
FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

cdp_config = create_facilitator_config()
x402_server = x402ResourceServer(
    HTTPFacilitatorClient(cdp_config)
)
x402_server.register("eip155:8453", ExactEvmServerScheme())
x402_server.register("eip155:137", ExactEvmServerScheme())
x402_server.register("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", ExactSvmServerScheme())
x402_server.register_extension(bazaar_resource_server_extension)

# Helper: create PaymentOption list for a given price (Base + Polygon + Solana)
def _pay_opts(price: str):
    return [
        PaymentOption(scheme="exact", price=price, network="eip155:8453", pay_to=WALLET_ADDRESS),
        PaymentOption(scheme="exact", price=price, network=POLYGON_NETWORK, pay_to=WALLET_ADDRESS),
        PaymentOption(scheme="exact", price=price, network=SOLANA_NETWORK, pay_to=SOLANA_WALLET),
    ]

x402_routes = {
    "GET /api/v1/kimchi-premium": RouteConfig(
        accepts=_pay_opts("$0.001"),
        description="Real-time Kimchi Premium for a single token (Upbit vs Binance via FX rate)",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"symbol": "BTC"},
            input_schema={
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Crypto symbol (e.g., BTC, ETH, XRP)",
                    }
                },
                "required": ["symbol"],
            },
            output=OutputConfig(
                example={
                    "symbol": "BTC",
                    "upbit_krw": 142000000,
                    "binance_usdt": 95200.5,
                    "fx_rate": 1475.27,
                    "fx_source": "exchangerate-api.com",
                    "binance_krw_equivalent": 140453370,
                    "premium_percent": 1.1,
                    "premium_direction": "positive",
                    "timestamp": 1776340000000,
                }
            ),
        ),
    ),
    "GET /api/v1/kr-prices": RouteConfig(
        accepts=_pay_opts("$0.001"),
        description="Korean exchange prices (Upbit, Bithumb) for a single token in KRW",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"symbol": "BTC", "exchange": "all"},
            input_schema={
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Crypto symbol (e.g., BTC, ETH)",
                    },
                    "exchange": {
                        "type": "string",
                        "enum": ["upbit", "bithumb", "all"],
                        "description": "Exchange to query",
                    },
                },
                "required": ["symbol"],
            },
            output=OutputConfig(
                example={
                    "symbol": "BTC",
                    "data": {
                        "upbit": {"exchange": "upbit", "symbol": "BTC", "price_krw": 142000000, "volume_24h": 1234567.89, "change_rate": 0.02, "data_age_seconds": 0},
                        "bithumb": {"exchange": "bithumb", "symbol": "BTC", "price_krw": 141950000, "volume_24h": 987654.32, "change_rate": 0.019, "data_age_seconds": 0},
                    },
                    "timestamp": 1776340000000,
                }
            ),
        ),
    ),
    "GET /api/v1/fx-rate": RouteConfig(
        accepts=_pay_opts("$0.001"),
        description="Current USD/KRW foreign exchange rate",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "base": "USD", "quote": "KRW", "rate": 1475.27,
                    "source": "exchangerate-api.com", "timestamp": 1776340000000, "data_age_seconds": 0,
                }
            ),
        ),
    ),
    "GET /api/v1/stablecoin-premium": RouteConfig(
        accepts=_pay_opts("$0.001"),
        description="USDT/USDC premium on Korean exchanges vs official USD/KRW rate — fund flow indicator",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "official_fx_rate": 1475.27,
                    "fx_source": "exchangerate-api.com",
                    "stablecoins": {
                        "usdt": {"price_krw": 1478, "premium_percent": 0.19, "premium_direction": "positive", "volume_24h": 50000000},
                        "usdc": {"price_krw": 1477, "premium_percent": 0.12, "premium_direction": "positive", "volume_24h": 30000000},
                    },
                    "interpretation": {"positive_premium": "Capital flowing INTO Korean crypto market", "negative_premium": "Capital flowing OUT of Korean crypto market"},
                    "timestamp": 1776340000000,
                }
            ),
        ),
    ),
    "GET /api/v1/arbitrage-scanner": RouteConfig(
        accepts=_pay_opts("$0.01"),
        description="Token-by-token Kimchi Premium for 189+ tokens, reverse premium, Upbit-Bithumb gaps, market share",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "premiums": [{"symbol": "BTC", "korean_name": "비트코인", "upbit_krw": 142000000, "binance_usd": 95200, "global_krw": 140453000, "premium_pct": 1.1, "warning": False, "caution_volume_soaring": False, "caution_deposit_soaring": False, "upbit_volume_krw": 500000000000}],
                    "reverse_premiums": [{"symbol": "SNT", "premium_pct": -63.8}],
                    "exchange_gaps": [{"symbol": "ETH", "upbit_krw": 3000000, "bithumb_krw": 3009000, "gap_pct": 0.3}],
                    "market_share": {"upbit_pct": 78.5, "bithumb_pct": 21.5, "upbit_volume_krw": 1500000000000, "bithumb_volume_krw": 410000000000},
                    "common_symbols_count": 189,
                    "fx_rate": 1475.27,
                    "meta": {"price": "$0.01", "update_interval": "60s"},
                }
            ),
        ),
    ),
    "GET /api/v1/exchange-alerts": RouteConfig(
        accepts=_pay_opts("$0.01"),
        description="Korean exchange alerts: new listings, delistings, investment warnings, caution flags",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "listing_changes": [{"symbol": "NEWTOKEN", "type": "NEW_LISTING", "korean_name": "뉴토큰", "detected_at": "2026-04-23T12:00:00Z"}],
                    "caution_tokens": [{"symbol": "RISK", "korean_name": "위험종목", "flags": ["INVESTMENT_WARNING", "VOLUME_SOARING"]}],
                    "meta": {"price": "$0.01", "update_interval": "60s"},
                }
            ),
        ),
    ),
    "GET /api/v1/market-movers": RouteConfig(
        accepts=_pay_opts("$0.01"),
        description="1-minute price surges/crashes, volume spikes, top 20 tokens by volume on Korean exchanges",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "movers_1m": [{"symbol": "SHIB", "prev_price": 1000, "curr_price": 1015, "change_1m_pct": 1.5, "volume_krw": 50000000000}],
                    "volume_spikes": [{"symbol": "WET", "volume_krw": 80000000000, "change_rate_24h": 3.5}],
                    "top_volume": [{"symbol": "BTC", "volume_krw": 500000000000, "change_rate": 0.02}],
                    "meta": {"price": "$0.01", "update_interval": "60s"},
                }
            ),
        ),
    ),
    "GET /api/v1/market-read": RouteConfig(
        accepts=_pay_opts("$0.1"),
        description="AI-powered Korean crypto market analysis — 12+ data sources + exchange intelligence + Claude AI token-level signals",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "signal": "BULLISH",
                    "confidence": "7/10",
                    "summary": "Korean market showing strong inflow signals with 1.1% average kimchi premium and rising stablecoin demand.",
                    "key_factors": ["BTC kimchi premium at 1.1%", "USDT premium positive at 0.19%", "Fear & Greed at 65 (Greed)", "Funding rate positive suggesting long bias"],
                    "token_alerts": ["WET: volume + deposit soaring = overheated, avoid longs", "BIO: new listing momentum, high volume"],
                    "risk_warning": "Elevated leverage in BTC futures with open interest at $12.5B.",
                    "data": {"korean_market": {}, "global_market": {}, "exchange_intelligence": {}},
                    "meta": {"price": "$0.10", "data_sources": ["upbit", "bithumb", "binance_futures", "coingecko", "alternative.me", "exchange_intelligence(180+tokens)"], "ai_model": "claude-haiku-4.5"},
                    "timestamp": 1776340000000,
                }
            ),
        ),
    ),
    "GET /api/v1/kr-sentiment": RouteConfig(
        accepts=_pay_opts("$0.05"),
        description="Korean crypto sentiment — AI analysis combining 189+ tokens exchange data with Korean news. First-in-world Korean-to-English crypto sentiment API.",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "sentiment": "CAUTIOUS_FOMO",
                    "score": 0.4,
                    "report_en": "Korean retail showing mixed signals with extreme reverse premiums on select tokens while deposit activity surges for mid-cap altcoins. Coinness reports increased institutional interest following regulatory clarity.",
                    "exchange_signals": {
                        "deposit_soaring": ["BIO", "ARKM", "HYPER"],
                        "volume_soaring": ["BIO", "ERA", "IN"],
                        "warnings": 2,
                        "avg_premium_pct": 0.3,
                        "extreme_premium_tokens": [{"symbol": "SNT", "premium_pct": -63.8}],
                    },
                    "news_context": {
                        "korean_related": [{"title": "업비트 신규 상장...", "timestamp": "2026-04-23T10:00:00Z"}],
                        "total_analyzed": 20,
                        "korean_count": 8,
                        "news_freshness_hours": 6,
                    },
                    "sources": ["Upbit API (189 tokens, real-time)", "Bithumb API (real-time)", "Binance API (reference)", "Coinness Telegram (20 articles analyzed, 8 Korean-related)"],
                    "timestamp": "2026-04-23T12:00:00Z",
                    "_meta": {"cache_age_seconds": 0, "computed_at": "2026-04-23T12:00:00Z", "data_sources_status": {"exchange_intel": "ok", "coinness": "ok"}},
                }
            ),
        ),
    ),
    "GET /api/v1/global-vs-korea-divergence": RouteConfig(
        # Light tier — divergence + 1-2 sentence AI summary. ~60s cache.
        accepts=_pay_opts("$0.05"),
        description="Global vs Korea divergence (light tier) — CoinGecko global price + Korean exchange + 1-2 sentence AI summary. For deeper analysis with Korean news signals and structured AI breakdown, use /api/v1/global-vs-korea-divergence-deep ($0.10).",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"symbol": "BTC"},
            input_schema={
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Crypto symbol (BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB)",
                    },
                },
                "required": ["symbol"],
            },
            output=OutputConfig(
                example={
                    "symbol": "BTC",
                    "korean_name": "비트코인",
                    "timestamp": 1777040000000,
                    "prices": {
                        "global_usd": 95200.50,
                        "global_source": "CoinGecko",
                        "korea_krw": 142000000,
                        "korea_source": "Upbit",
                        "fx_rate": 1481.45,
                        "fx_source": "exchangerate-api.com",
                    },
                    "divergence": {
                        "korea_implied_usd": 95850.00,
                        "premium_pct": 0.68,
                        "direction": "positive",
                        "magnitude": "small",
                    },
                    "context_signals": {
                        "investment_warning": False,
                        "volume_spike_24h": False,
                        "global_volume_change_pct": 12.3,
                    },
                    "ai_interpretation": "Korean market shows a small positive premium of 0.68% over global pricing with no active investment warning, suggesting modest local demand without overheating signals.",
                    "data_age_seconds": 0,
                    "depth": "light",
                }
            ),
        ),
    ),
    "GET /api/v1/global-vs-korea-divergence-deep": RouteConfig(
        # Deep tier — light data + Korean news signal + structured AI analysis. ~5min cache.
        accepts=_pay_opts("$0.10"),
        description="Global vs Korea divergence (deep tier) — light response + Korean news signals (Coinness Telegram, 24h window) + structured AI analysis (drivers, global context, action suggestion, confidence). For lighter/cheaper analysis, use /api/v1/global-vs-korea-divergence ($0.05).",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"symbol": "BTC"},
            input_schema={
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Crypto symbol (BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB)",
                    },
                },
                "required": ["symbol"],
            },
            output=OutputConfig(
                example={
                    "symbol": "BTC",
                    "korean_name": "비트코인",
                    "timestamp": 1777040000000,
                    "prices": {
                        "global_usd": 95200.50, "global_source": "CoinGecko",
                        "korea_krw": 142000000, "korea_source": "Upbit",
                        "fx_rate": 1481.45, "fx_source": "exchangerate-api.com",
                    },
                    "divergence": {
                        "korea_implied_usd": 95850.00, "premium_pct": 0.68,
                        "direction": "positive", "magnitude": "small",
                    },
                    "context_signals": {
                        "investment_warning": False, "volume_spike_24h": False,
                        "global_volume_change_pct": 12.3,
                    },
                    "recent_news_signal": {
                        "korean_news_count_24h": 4,
                        "sentiment_score": 0.6,
                        "top_keywords": ["BTC", "달러", "선물"],
                        "source": "Coinness Telegram",
                    },
                    "ai_deep_analysis": {
                        "summary": "BTC shows a modest positive Korea premium with steady but not elevated market attention.",
                        "korean_market_drivers": ["Dollar strength considerations", "Futures market activity", "Stable positive sentiment"],
                        "global_context": "BTC tracking global trends without significant divergence.",
                        "implied_action_suggestion": "Premium is modest; insufficient signal alone for directional bias.",
                        "confidence": "medium",
                    },
                    "data_age_seconds": 0,
                    "depth": "deep",
                }
            ),
        ),
    ),
}

app = FastAPI(
    title="KR Crypto Intelligence API",
    description="Korean crypto market data + AI sentiment for AI agents. Kimchi premium, exchange intelligence, sentiment analysis.",
    version="0.1.0",
    lifespan=lifespan
)

# x402 결제 미들웨어 적용
app.add_middleware(PaymentMiddlewareASGI, routes=x402_routes, server=x402_server)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/docs", "/openapi.json", "/", "/favicon.ico", "/llms.txt", "/.well-known/x402"):
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
            "/api/v1/kr-sentiment": "Korean crypto sentiment — AI analysis + news ($0.05)",
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
        "description": "Korean crypto market data + AI analysis for AI agents. 13 endpoints, 189+ tokens. Kimchi Premium, exchange intelligence, AI sentiment, divergence analysis (light/deep), market read.",
        "url": "https://api.printmoneylab.com",
        "mcp": "https://mcp.printmoneylab.com/mcp",
        "source": "https://github.com/bakyang2/kr-crypto-intelligence",
        "llms_txt": "https://api.printmoneylab.com/llms.txt",
        "endpoints": [
            {"path": "/api/v1/kimchi-premium", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Real-time Kimchi Premium (Upbit vs Binance)"},
            {"path": "/api/v1/kr-prices", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean exchange prices (Upbit, Bithumb)"},
            {"path": "/api/v1/fx-rate", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "USD/KRW exchange rate"},
            {"path": "/api/v1/stablecoin-premium", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "USDT/USDC premium on Korean exchanges (fund flow indicator)"},
            {"path": "/api/v1/arbitrage-scanner", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Token-by-token Kimchi Premium for 189+ tokens, reverse premium, Upbit-Bithumb gaps, market share"},
            {"path": "/api/v1/exchange-alerts", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "New listings/delistings, investment warnings, caution flags"},
            {"path": "/api/v1/market-movers", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "1-min price surges/crashes, volume spikes, top volume tokens"},
            {"path": "/api/v1/market-read", "method": "GET", "price": "$0.10", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "AI market analysis — 12+ data sources + exchange intelligence + Claude AI token-level signals"},
            {"path": "/api/v1/kr-sentiment", "method": "GET", "price": "$0.05", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean crypto sentiment — exchange intelligence + Korean news + AI analysis. First-in-world Korean-to-English crypto sentiment API"},
            {"path": "/api/v1/global-vs-korea-divergence", "method": "GET", "price": "$0.05", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Global vs Korea price divergence (light) — CoinGecko global price + Korean exchange + 1-2 sentence AI summary"},
            {"path": "/api/v1/global-vs-korea-divergence-deep", "method": "GET", "price": "$0.10", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Global vs Korea price divergence (deep) — light response + Korean news signals (Coinness Telegram) + structured AI analysis"}
        ],
        "free_endpoints": [
            {"path": "/api/v1/symbols", "method": "GET", "description": "Available trading symbols"},
            {"path": "/health", "method": "GET", "description": "Service health check"},
            {"path": "/api/v1/stats", "method": "GET", "description": "API usage statistics"}
        ],
        "payment": [
            {"scheme": "exact", "network": "eip155:8453", "asset": "USDC", "payTo": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"},
            {"scheme": "exact", "network": "eip155:137", "asset": "USDC", "payTo": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"},
            {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "asset": "USDC", "payTo": "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9"}
        ],
        "tags": ["korean", "crypto", "kimchi-premium", "upbit", "bithumb", "fx-rate", "market-data", "asia", "arbitrage", "exchange-intelligence", "ai-analysis", "divergence", "coingecko"]
    }

LLMS_TXT_CONTENT = """# KR Crypto Intelligence API

> Korean crypto market data + AI sentiment analysis for AI agents. Pay per request via x402 on Base, Polygon, and Solana.
> API: https://api.printmoneylab.com
> MCP: https://mcp.printmoneylab.com/mcp
> Docs: https://api.printmoneylab.com/docs
> GitHub: https://github.com/bakyang2/kr-crypto-intelligence

## How it works

Every endpoint is callable via the x402 payment protocol.
Your agent sends a standard HTTP request; when payment is required,
it receives HTTP 402 with payment instructions. No API keys, no accounts,
no registration. Payment is settled per-request in USDC.

## Unique value

World's first Korean-to-English crypto sentiment API. Covers 189+ tokens
across Upbit and Bithumb (top Korean exchanges). Academic research
(European Journal of Finance, 2026) confirms Korean news sentiment
predicts global crypto returns.

## Networks supported

- Base (eip155:8453)
- Polygon (eip155:137)
- Solana mainnet

## Endpoints

### Korean Sentiment Analysis
- GET /api/v1/kr-sentiment -> $0.05
  World's first Korean-to-English crypto sentiment. Combines 189+ tokens
  exchange intelligence with Korean news context for AI-powered insights.
  1-hour cache.

### AI Analysis
- GET /api/v1/market-read -> $0.10
  Comprehensive market analysis combining 12+ data sources with
  Claude AI. Returns signal (BULLISH/BEARISH/NEUTRAL), confidence
  score, token-level alerts.
- GET /api/v1/global-vs-korea-divergence?symbol={SYMBOL} -> $0.05
  Global (CoinGecko) vs Korean (Upbit) price divergence with AI
  interpretation (1-2 sentence summary). 25 supported symbols
  (BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM,
  UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC,
  HBAR, SHIB).
- GET /api/v1/global-vs-korea-divergence-deep?symbol={SYMBOL} -> $0.10
  Same as light tier plus: Korean news signals from Coinness Telegram
  (24h window, top keywords, sentiment score) and structured AI
  analysis with korean_market_drivers, global_context, action
  suggestion, and confidence rating.

### Korean Exchange Intelligence
- GET /api/v1/arbitrage-scanner -> $0.01
  Token-by-token Kimchi Premium for 189+ tokens, reverse premium
  detection, Upbit-Bithumb price gaps.
- GET /api/v1/exchange-alerts -> $0.01
  New listings/delistings, investment warnings, caution flags.
- GET /api/v1/market-movers -> $0.01
  1-minute price surges/crashes, volume spikes, top 20 by volume.

### Market Data
- GET /api/v1/kimchi-premium?symbol={SYMBOL} -> $0.001
- GET /api/v1/stablecoin-premium -> $0.001
- GET /api/v1/kr-prices?symbol={SYMBOL}&exchange={EXCHANGE} -> $0.001
- GET /api/v1/fx-rate -> $0.001

### Free
- GET /api/v1/symbols -> free (list of tradeable symbols)
- GET /health -> free (service health)

## Discovery

Our endpoints are registered in the x402 Bazaar discovery layer.
Query all CDP-facilitated services:
  GET https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources

## MCP server

Connect any MCP-compatible AI agent (Claude, Cursor, ChatGPT):
  URL: https://mcp.printmoneylab.com/mcp
  Transport: streamable-http
  Tools: 11 (get_kr_sentiment, get_market_read, get_arbitrage_scanner,
         get_exchange_alerts, get_market_movers, get_kimchi_premium,
         get_stablecoin_premium, get_kr_prices, get_fx_rate,
         get_available_symbols, check_health)
"""

@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    return LLMS_TXT_CONTENT

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
    log_event("api_call", endpoint="kimchi-premium", paid=True, price_usd=0.001)
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
    log_event("api_call", endpoint="stablecoin-premium", paid=True, price_usd=0.001)
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
    log_event("api_call", endpoint="kr-prices", paid=True, price_usd=0.001)
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
    log_event("api_call", endpoint="fx-rate", paid=True, price_usd=0.001)
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

# === kr-sentiment endpoint ===
@app.get("/api/v1/kr-sentiment")
async def kr_sentiment_endpoint(request: Request):
    """Korean crypto market sentiment — AI analysis combining exchange data + Korean news."""
    track_request("/api/v1/kr-sentiment")
    ip = get_real_ip(request)
    try:
        # Claude 이상 감지 알림은 비결제 이벤트 — 플래그 off 시 tg_send 전달 안 함
        anomaly_sender = tg_send if ENABLE_REALTIME_NON_PAYMENT_ALERTS else None
        result = await handle_kr_sentiment(tg_send_func=anomaly_sender)
        log_event("api_call", endpoint="kr-sentiment", paid=True, price_usd=0.05)
        # Note: 텔레그램 알림은 미들웨어(line ~655)에서 일괄 발송 — 중복 카운트 방지
        return result
    except Exception as e:
        stats["errors"] += 1
        log_event("error", endpoint="kr-sentiment", error=str(e)[:200])
        raise HTTPException(status_code=503, detail=f"Sentiment analysis failed: {str(e)[:100]}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


# ============================================================
# Market Read - AI-powered Korean crypto market analysis
# ============================================================

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# 단일 Anthropic 클라이언트 — 모듈 레벨, thread-safe 공식 보장.
# 매 호출마다 새 인스턴스 생성 시 connection pool 재초기화로 200~400ms 손실.
# timeout=30초로 통일 (deep 호출 기준 가장 큰 값).
ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30.0)

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
        message = ANTHROPIC_CLIENT.messages.create(
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
    log_event("api_call", endpoint="arbitrage-scanner", paid=True, price_usd=0.01)
    ip = get_real_ip(request)
    data = compute_intel_data()
    if not data:
        raise HTTPException(status_code=503, detail="Intel data not ready yet. Try again in 60 seconds.")
    # 텔레그램 알림은 미들웨어에서 일괄 발송
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
    log_event("api_call", endpoint="exchange-alerts", paid=True, price_usd=0.01)
    ip = get_real_ip(request)
    data = compute_intel_data()
    if not data:
        raise HTTPException(status_code=503, detail="Intel data not ready yet. Try again in 60 seconds.")
    # 텔레그램 알림은 미들웨어에서 일괄 발송
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
    log_event("api_call", endpoint="market-movers", paid=True, price_usd=0.01)
    ip = get_real_ip(request)
    data = compute_intel_data()
    if not data:
        raise HTTPException(status_code=503, detail="Intel data not ready yet. Try again in 60 seconds.")
    # 텔레그램 알림은 미들웨어에서 일괄 발송
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
    log_event("api_call", endpoint="market-read", paid=True, price_usd=0.10)

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

        # 텔레그램 알림은 미들웨어에서 일괄 발송
        return response

    except Exception as e:
        print(f"[ERR] market-read: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


# ============================================================
# Global vs Korea Divergence — CoinGecko + Korean exchange + AI
# ============================================================

# Symbol → CoinGecko coin id
COINGECKO_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple", "SOL": "solana",
    "ADA": "cardano", "DOGE": "dogecoin", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "AVAX": "avalanche-2",
    "ATOM": "cosmos", "UNI": "uniswap", "LTC": "litecoin", "NEAR": "near",
    "OP": "optimism", "ARB": "arbitrum", "APT": "aptos", "ALGO": "algorand",
    "FTM": "fantom", "SUI": "sui", "TRX": "tron", "BCH": "bitcoin-cash",
    "ETC": "ethereum-classic", "HBAR": "hedera-hashgraph", "SHIB": "shiba-inu",
}

# Symbol → 한국어 이름 (best-effort)
KOREAN_NAME_MAP = {
    "BTC": "비트코인", "ETH": "이더리움", "XRP": "리플", "SOL": "솔라나",
    "ADA": "카르다노", "DOGE": "도지코인", "DOT": "폴카닷",
    "MATIC": "폴리곤", "LINK": "체인링크", "AVAX": "아발란체",
    "ATOM": "코스모스", "UNI": "유니스왑", "LTC": "라이트코인", "NEAR": "니어",
    "OP": "옵티미즘", "ARB": "아비트럼", "APT": "앱토스", "ALGO": "알고랜드",
    "FTM": "팬텀", "SUI": "수이", "TRX": "트론", "BCH": "비트코인캐시",
    "ETC": "이더리움클래식", "HBAR": "헤데라", "SHIB": "시바이누",
}

# Cache: f"divergence:{symbol}:{depth}" -> (data, expires_at)
_divergence_cache = {}
_divergence_locks = {}  # per-key asyncio.Lock
DIVERGENCE_LIGHT_TTL = 60
DIVERGENCE_DEEP_TTL = 300


def _get_divergence_lock(key: str):
    if key not in _divergence_locks:
        _divergence_locks[key] = asyncio.Lock()
    return _divergence_locks[key]


async def fetch_coingecko_price(symbol: str) -> dict | None:
    """CoinGecko simple/price 조회. 60초 캐시."""
    coin_id = COINGECKO_ID_MAP.get(symbol)
    if not coin_id:
        return None

    cache_key = f"coingecko:{symbol}"
    cached, age = get_cache(cache_key)
    if cached:
        cached["data_age_seconds"] = round(age, 1)
        return cached

    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": coin_id,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_24hr_vol": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if coin_id not in data:
                return None
            entry = data[coin_id]
            result = {
                "coin_id": coin_id,
                "price_usd": float(entry.get("usd", 0)),
                "change_24h_pct": float(entry.get("usd_24h_change", 0) or 0),
                "volume_24h_usd": float(entry.get("usd_24h_vol", 0) or 0),
                "data_age_seconds": 0,
            }
            set_cache(cache_key, result)
            return result
    except Exception as e:
        print(f"[DIVERGENCE] CoinGecko fetch failed for {symbol}: {e}")
        return None


def classify_magnitude(premium_pct: float) -> str:
    a = abs(premium_pct)
    if a < 1.0:
        return "small"
    if a < 3.0:
        return "moderate"
    return "large"


def classify_direction(premium_pct: float) -> str:
    if abs(premium_pct) < 0.1:
        return "neutral"
    return "positive" if premium_pct > 0 else "negative"


def compute_volume_spike(symbol: str, current_volume_24h: float) -> bool:
    """Upbit 24h volume이 기존 캐시된 단기 평균의 1.5배를 초과하면 True.
    7일 평균 데이터는 별도 시계열이 없으므로, intel_cache의 prev_upbit_tickers를
    근사 기준으로 사용 (직전 사이클 vs 현재 = 단기 변동 감지)."""
    try:
        prev = intel_cache.get("prev_upbit_tickers", {})
        prev_data = prev.get(symbol)
        if not prev_data:
            return False
        prev_vol = prev_data.get("volume_24h", 0)
        if prev_vol <= 0:
            return False
        # 24h volume이 직전 사이클(보통 1분 전) 대비 +50% 이상이면 spike
        return current_volume_24h > prev_vol * 1.5
    except Exception:
        return False


def get_investment_warning(symbol: str) -> bool:
    """intel_cache의 upbit_market_details에서 해당 symbol의 warning 플래그 확인."""
    try:
        details = intel_cache.get("upbit_market_details", {})
        d = details.get(symbol, {})
        return bool(d.get("warning", False))
    except Exception:
        return False


# === Coinness 24h 뉴스 백그라운드 캐시 (5분 TTL, 모든 25개 심볼 공유) ===
# Coinness는 전체 한국 시장 뉴스라 심볼 무관 — 매 deep 요청마다 fetch하면 1~2초 손실.
# 5분마다 백그라운드 task가 갱신, 핸들러는 메모리에서만 읽음 (논블로킹).
_coinness_news_cache = {
    "messages": [],         # list of {"text": str, "timestamp": str}
    "fetched_at": 0,        # unix ts of last successful fetch
    "status": "pending",    # "ok" | "stale" | "pending" | "failed"
}
COINNESS_REFRESH_INTERVAL = 300  # 5분


async def coinness_news_poller():
    """startup 시 1회 fetch + 5분마다 갱신. compute_divergence는 캐시만 읽음."""
    while True:
        try:
            from kr_sentiment import fetch_coinness_news
            news = await fetch_coinness_news(hours=24)
            _coinness_news_cache["messages"] = news
            _coinness_news_cache["fetched_at"] = time.time()
            _coinness_news_cache["status"] = "ok"
            print(f"[DIVERGENCE] Coinness cache refreshed: {len(news)} messages")
        except Exception as e:
            print(f"[DIVERGENCE] Coinness cache refresh failed: {e}")
            # 이전 데이터가 있으면 stale로 표시, 없으면 failed
            if _coinness_news_cache["messages"]:
                _coinness_news_cache["status"] = "stale"
            else:
                _coinness_news_cache["status"] = "failed"
        await asyncio.sleep(COINNESS_REFRESH_INTERVAL)


def get_news_signal_for_symbol(symbol: str, korean_name: str) -> dict:
    """캐시된 Coinness 뉴스에서 symbol/한국어명 매칭 추출 (deep 전용, 동기 함수).
    캐시 누락(failed) 시 빈 시그널 반환."""
    news = _coinness_news_cache["messages"]
    status = _coinness_news_cache["status"]

    if not news or status == "failed":
        return {
            "korean_news_count_24h": 0,
            "sentiment_score": 0.0,
            "top_keywords": [],
            "source": "Coinness Telegram (unavailable)",
        }

    sym_lower = symbol.lower()
    name_terms = [korean_name] if korean_name else []
    matched_texts = []
    for n in news:
        t = n.get("text", "")
        t_lower = t.lower()
        if sym_lower in t_lower or any(name in t for name in name_terms):
            matched_texts.append(t)

    keywords = _extract_top_keywords(matched_texts, top_n=3)
    sentiment = _simple_sentiment(matched_texts)

    source_label = "Coinness Telegram" if status == "ok" else "Coinness Telegram (stale)"
    return {
        "korean_news_count_24h": len(matched_texts),
        "sentiment_score": round(sentiment, 2),
        "top_keywords": keywords,
        "source": source_label,
    }


_POSITIVE_KW = ["상승", "급등", "호재", "매수", "기관", "ETF", "승인", "상장", "신고가", "돌파", "랠리"]
_NEGATIVE_KW = ["하락", "급락", "악재", "매도", "규제", "거부", "상장폐지", "신저가", "패닉", "청산"]
_STOP = {"이", "그", "저", "것", "수", "등", "및", "의", "에", "를", "은", "는", "가", "이다", "있다", "한다"}


def _extract_top_keywords(texts: list[str], top_n: int = 3) -> list[str]:
    if not texts:
        return []
    counts = {}
    word_pattern = re.compile(r"[가-힣A-Z]{2,10}")
    for t in texts:
        for w in word_pattern.findall(t):
            if w in _STOP:
                continue
            counts[w] = counts.get(w, 0) + 1
    sorted_kw = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [kw for kw, _ in sorted_kw[:top_n]]


def _simple_sentiment(texts: list[str]) -> float:
    if not texts:
        return 0.0
    pos = neg = 0
    for t in texts:
        for kw in _POSITIVE_KW:
            if kw in t:
                pos += 1
        for kw in _NEGATIVE_KW:
            if kw in t:
                neg += 1
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def call_claude_divergence_light(symbol: str, premium_pct: float, direction: str,
                                  warning: bool, volume_spike: bool) -> str | None:
    """Light AI 해석 — 단일 영문 paragraph (1-2 문장)."""
    prompt = f"""You are an analyst summarizing Korea vs global crypto divergence in 1-2 sentences.
Data:

Symbol: {symbol}
Korea premium: {premium_pct}%
Direction: {direction}
Investment warning active: {warning}
Volume spike 24h: {volume_spike}

Output: Single English paragraph, factual, no investment advice."""
    try:
        msg = ANTHROPIC_CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"[DIVERGENCE] Claude light failed: {e}")
        return None


def call_claude_divergence_deep(symbol: str, premium_pct: float, direction: str,
                                 warning: bool, volume_spike: bool,
                                 news_count: int, keywords: list[str],
                                 sentiment_score: float) -> dict | None:
    """Deep AI 해석 — JSON 응답."""
    prompt = f"""You are a Korea-focused crypto market analyst. Produce structured analysis.
Inputs:

Symbol: {symbol}
Korea premium: {premium_pct}% ({direction})
Korea volume spike: {volume_spike}
Investment warning: {warning}
Korean news count 24h: {news_count}
Top keywords: {keywords}
News sentiment: {sentiment_score}

Output JSON only (no markdown):
{{"summary":"2-3 sentence overview","korean_market_drivers":["bullet 1","bullet 2","bullet 3"],"global_context":"1 sentence","implied_action_suggestion":"factual statement, not financial advice","confidence":"low|medium|high"}}"""
    try:
        msg = ANTHROPIC_CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as e:
        print(f"[DIVERGENCE] Claude deep failed: {e}")
        return None


async def compute_divergence(symbol: str, depth: str) -> dict:
    """Light/deep 모두 처리. CoinGecko + Upbit + FX + AI."""
    coin_id = COINGECKO_ID_MAP.get(symbol)
    korean_name = KOREAN_NAME_MAP.get(symbol, "")

    # 병렬 fetch: CoinGecko, Upbit, FX
    cg_task = fetch_coingecko_price(symbol)
    upbit_task = fetch_upbit_price(symbol)
    fx_task = fetch_fx_rate()
    cg, upbit, fx = await asyncio.gather(cg_task, upbit_task, fx_task, return_exceptions=True)

    if isinstance(cg, Exception) or cg is None:
        # CoinGecko 실패 — stale cache 시도
        stale, _ = get_cache(f"coingecko:{symbol}")
        if not stale:
            raise HTTPException(status_code=503, detail="CoinGecko unavailable and no cached data. Try again shortly.")
        cg = stale

    if isinstance(upbit, Exception) or upbit is None or "error" in (upbit or {}):
        raise HTTPException(status_code=503, detail=f"Upbit price unavailable for {symbol}.")

    if isinstance(fx, Exception) or fx is None:
        raise HTTPException(status_code=503, detail="FX rate unavailable.")

    global_usd = cg["price_usd"]
    korea_krw = upbit["price_krw"]
    fx_rate = fx["rate"]
    fx_source = fx["source"]

    if global_usd <= 0 or fx_rate <= 0:
        raise HTTPException(status_code=503, detail="Invalid price/fx data.")

    korea_implied_usd = round(korea_krw / fx_rate, 2)
    global_krw_equiv = global_usd * fx_rate
    premium_pct = round(((korea_krw - global_krw_equiv) / global_krw_equiv) * 100, 3)
    direction = classify_direction(premium_pct)
    magnitude = classify_magnitude(premium_pct)

    warning = get_investment_warning(symbol)
    volume_spike = compute_volume_spike(symbol, upbit.get("volume_24h", 0) or 0)

    response = {
        "symbol": symbol,
        "korean_name": korean_name,
        "timestamp": int(time.time() * 1000),
        "prices": {
            "global_usd": global_usd,
            "global_source": "CoinGecko",
            "korea_krw": korea_krw,
            "korea_source": "Upbit",
            "fx_rate": fx_rate,
            "fx_source": fx_source,
        },
        "divergence": {
            "korea_implied_usd": korea_implied_usd,
            "premium_pct": premium_pct,
            "direction": direction,
            "magnitude": magnitude,
        },
        "context_signals": {
            "investment_warning": warning,
            "volume_spike_24h": volume_spike,
            "global_volume_change_pct": round(cg.get("change_24h_pct", 0), 2),
        },
        "data_age_seconds": cg.get("data_age_seconds", 0),
        "depth": depth,
    }

    # AI 해석 — light 또는 deep
    loop = asyncio.get_event_loop()
    if depth == "light":
        ai_text = await loop.run_in_executor(
            None, call_claude_divergence_light,
            symbol, premium_pct, direction, warning, volume_spike,
        )
        response["ai_interpretation"] = ai_text  # may be None on error
    else:  # deep
        # 뉴스 시그널은 백그라운드 캐시에서 즉시 읽음 (블로킹 없음)
        news_signal = get_news_signal_for_symbol(symbol, korean_name)
        response["recent_news_signal"] = news_signal

        # Deep tier는 ai_deep_analysis 1회만 호출 (이전: light + deep 2회 → 50% 단축)
        deep_obj = await loop.run_in_executor(
            None, call_claude_divergence_deep,
            symbol, premium_pct, direction, warning, volume_spike,
            news_signal["korean_news_count_24h"],
            news_signal["top_keywords"],
            news_signal["sentiment_score"],
        )
        response["ai_deep_analysis"] = deep_obj  # may be None

    return response


async def _serve_divergence(symbol: str, depth_norm: str, endpoint_label: str, price_usd: float, ttl: int):
    """Shared light/deep dispatcher with per-key cache + lock."""
    sym = validate_symbol(symbol)
    if sym not in COINGECKO_ID_MAP:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsupported symbol", "supported": sorted(COINGECKO_ID_MAP.keys())},
        )

    cache_key = f"divergence:{sym}:{depth_norm}"

    # Fast cache hit
    now = time.time()
    cached_entry = _divergence_cache.get(cache_key)
    if cached_entry and now < cached_entry[1]:
        data = dict(cached_entry[0])
        data["data_age_seconds"] = int(now - (cached_entry[1] - ttl))
        log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd)
        return data

    # Slow path with per-key lock
    lock = _get_divergence_lock(cache_key)
    async with lock:
        now = time.time()
        cached_entry = _divergence_cache.get(cache_key)
        if cached_entry and now < cached_entry[1]:
            data = dict(cached_entry[0])
            data["data_age_seconds"] = int(now - (cached_entry[1] - ttl))
            log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd)
            return data

        try:
            result = await compute_divergence(sym, depth_norm)
        except HTTPException:
            stats["errors"] += 1
            raise
        except Exception as e:
            stats["errors"] += 1
            print(f"[DIVERGENCE] compute error: {e}")
            log_event("error", endpoint=endpoint_label, error=str(e)[:200])
            raise HTTPException(status_code=503, detail=f"Divergence computation failed: {str(e)[:100]}")

        _divergence_cache[cache_key] = (result, time.time() + ttl)
        log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd)
        return result


@app.get("/api/v1/global-vs-korea-divergence")
async def global_vs_korea_divergence(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    """Light tier — divergence + 1-2 sentence AI summary. $0.05."""
    track_request("/api/v1/global-vs-korea-divergence")
    return await _serve_divergence(symbol, "light", "global-vs-korea-divergence", 0.05, DIVERGENCE_LIGHT_TTL)


@app.get("/api/v1/global-vs-korea-divergence-deep")
async def global_vs_korea_divergence_deep(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    """Deep tier — light data + Korean news signal + structured AI analysis. $0.10."""
    track_request("/api/v1/global-vs-korea-divergence-deep")
    return await _serve_divergence(symbol, "deep", "global-vs-korea-divergence-deep", 0.10, DIVERGENCE_DEEP_TTL)
