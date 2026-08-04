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
from patch_exchange_intel import intel_cache, compute_intel_data, intel_polling_task, load_alert_history, tg_bot_polling, calculate_cdp_cost
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

# === XRPL (t54 facilitator) ===
# Path C — isolated to /api/v1/xrpl/* routes. Uses its own middleware
# (require_payment) and its own PAYMENT-SIGNATURE header (not X-PAYMENT).
# Coexists with PaymentMiddlewareASGI without interference — the two
# middlewares watch different paths and different headers.
from x402_xrpl.server import require_payment as _xrpl_require_payment

from stats_logger import log_event, aggregate_stats, aggregate_stats_range, maybe_archive
from kr_sentiment import handle_kr_sentiment, load_cache_from_disk as load_sentiment_cache
from kr_news import fetch_kr_news
from krw_macro import fetch_krw_macro_stress
from merchant_ops import (
    create_receipt as _create_receipt,
    send_post_settle_alert as _send_post_settle_alert,
    log_post_settle_failure as _log_post_settle_failure,
    aggregate_post_settle_failures as _aggregate_post_settle_failures,
    render_post_settle_summary_lines as _render_post_settle_summary_lines,
    aggregate_mcp_calls as _aggregate_mcp_calls,
    render_mcp_summary_lines as _render_mcp_summary_lines,
    ENDPOINT_PRICES as _ENDPOINT_PRICES,
    SIGNER_ADDRESS as _RECEIPT_SIGNER_ADDRESS,
    SIGNER_PUBLIC_KEY as _RECEIPT_SIGNER_PUBLIC_KEY,
)

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

# IP 분류 캐시 (24시간 TTL)
_ip_classification_cache = {}

# 일일 사용자 결제 카운트 (자정 KST 리셋)
_daily_user_calls = defaultdict(int)
_daily_user_revenue = defaultdict(float)
_daily_reset_kst_date = None  # YYYY-MM-DD 형식


async def classify_ip(ip: str) -> dict:
    """IP 분류: residential / datacenter / unknown.
    무료 ipinfo.io API 사용 (인증 없이 50req/day, 1일 캐시)."""
    if ip in _ip_classification_cache:
        cached = _ip_classification_cache[ip]
        if time.time() - cached["ts"] < 86400:
            return cached["data"]

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"https://ipinfo.io/{ip}/json")
            data = r.json()
            org = data.get("org", "").lower()
            is_dc = any(x in org for x in [
                "amazon", "aws", "google llc", "google cloud", "microsoft", "azure",
                "digitalocean", "cloudflare", "render", "hetzner",
                "linode", "vultr", "ovh", "oracle"
            ])
            result = {
                "type": "datacenter" if is_dc else "residential",
                "city": data.get("city", "?"),
                "country": data.get("country", "?"),
                "org_short": data.get("org", "?")[:40],
            }
    except Exception:
        result = {"type": "unknown", "city": "?", "country": "?", "org_short": "?"}

    _ip_classification_cache[ip] = {"data": result, "ts": time.time()}
    return result


# 누적 결제 내역 캐시 (IP별, 1분 TTL — stats.jsonl 재파싱 부담 줄임)
_user_history_cache = {}
STATS_JSONL_PATH = os.getenv("STATS_JSONL_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.jsonl"))

# 본인(Moa) 검증 결제 IP — 진성 사용자 카운트에서 제외.
# KR Broadband 가정 IP 2개. 운영자 직접 결제 테스트용.
_OWNER_IPS = {"118.40.115.95", "1.249.16.154"}


def get_user_history(ip: str) -> dict:
    """stats.jsonl을 파싱해서 IP별 누적 결제 카운트 + 매출 + 첫 결제 날짜 반환.
    5초 메모리 캐시 (1분 → 5초로 단축, 같은 IP에서 1분 안에 여러 결제 시 누적 갱신 보장).
    파일 누락/IP 누락 시 빈 결과 반환 (graceful)."""
    if ip in _user_history_cache:
        cached = _user_history_cache[ip]
        if time.time() - cached["ts"] < 5:
            return cached["data"]

    total_calls = 0
    total_revenue = 0.0
    first_seen_ts = None

    try:
        with open(STATS_JSONL_PATH, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if (entry.get("type") == "api_call"
                    and entry.get("paid")
                    and entry.get("ip") == ip):
                    total_calls += 1
                    total_revenue += entry.get("price_usd", 0)
                    ts = entry.get("ts")
                    if ts and (first_seen_ts is None or ts < first_seen_ts):
                        first_seen_ts = ts
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[USER-HISTORY] read error: {e}")

    first_seen_date = None
    days_ago = None
    if first_seen_ts:
        kst = timezone(timedelta(hours=9))
        first_date = datetime.fromtimestamp(first_seen_ts, kst)
        today_kst = datetime.now(kst)
        days_ago = (today_kst.date() - first_date.date()).days
        first_seen_date = first_date.strftime("%m월 %d일")

    result = {
        "total_calls": total_calls,
        "total_revenue": round(total_revenue, 4),
        "first_seen_ts": first_seen_ts,
        "first_seen_date": first_seen_date,
        "days_ago": days_ago,
    }
    _user_history_cache[ip] = {"data": result, "ts": time.time()}
    return result


async def daily_user_stats_reset():
    """매일 KST 00:00에 일일 사용자 통계 리셋. 1시간 주기 체크."""
    global _daily_reset_kst_date
    while True:
        try:
            now_kst = datetime.now(timezone(timedelta(hours=9)))
            today_kst = now_kst.strftime("%Y-%m-%d")
            if _daily_reset_kst_date != today_kst:
                _daily_user_calls.clear()
                _daily_user_revenue.clear()
                _daily_reset_kst_date = today_kst
        except Exception as e:
            print(f"[DAILY-RESET] error: {e}")
        await asyncio.sleep(3600)


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

PAID_ENDPOINTS_LIST = [
    "/api/v1/kimchi-premium", "/api/v1/kr-prices", "/api/v1/fx-rate", "/api/v1/stablecoin-premium",
    "/api/v1/market-read", "/api/v1/arbitrage-scanner", "/api/v1/exchange-alerts", "/api/v1/market-movers",
    "/api/v1/kr-sentiment", "/api/v1/global-vs-korea-divergence", "/api/v1/global-vs-korea-divergence-deep",
    "/api/v1/kr-news/kpop", "/api/v1/kr-news/kpop-summary", "/api/v1/kr-news/semiconductor",
    "/api/v1/kr-news/semiconductor-summary",
    "/api/v1/krw-macro-stress",
    # XRPL variants (Path C) — RLUSD via t54 mainnet facilitator
    "/api/v1/xrpl/kimchi-premium", "/api/v1/xrpl/kr-prices", "/api/v1/xrpl/fx-rate", "/api/v1/xrpl/stablecoin-premium",
    "/api/v1/xrpl/market-read", "/api/v1/xrpl/arbitrage-scanner", "/api/v1/xrpl/exchange-alerts",
    "/api/v1/xrpl/market-movers", "/api/v1/xrpl/kr-sentiment", "/api/v1/xrpl/global-vs-korea-divergence",
    "/api/v1/xrpl/global-vs-korea-divergence-deep", "/api/v1/xrpl/kr-news/kpop",
    "/api/v1/xrpl/kr-news/kpop-summary", "/api/v1/xrpl/kr-news/semiconductor",
    "/api/v1/xrpl/kr-news/semiconductor-summary",
    "/api/v1/xrpl/krw-macro-stress",
]


def _network_label(network: str) -> str:
    """CAIP-2 network id → 사람이 읽을 수 있는 짧은 라벨."""
    if not network:
        return "unknown"
    n = network.lower()
    if n.startswith("eip155:8453"):
        return "Base"
    if n.startswith("eip155:137"):
        return "Polygon"
    if n.startswith("solana:"):
        return "Solana"
    if n.startswith("xrpl:"):
        return "XRPL"
    if n.startswith("eip155:"):
        return f"EVM({network.split(':',1)[1]})"
    return network


async def tg_notify_request(endpoint, symbol, ip, status_code=200, network=None, payer=None, transaction=None):
    """결제 settle 마다 payment_settled 이벤트 기록 + 5분 dedupe 후 텔레그램 알림 발송.

    핵심 설계: payment_settled 로깅과 텔레그램 알림 dedupe 를 분리.
      - log_event("payment_settled", ...) 는 매 settle 마다 무조건 1회 실행
      - 텔레그램 알림 발송만 (endpoint, ip, network, payer) 5분 dedupe 적용
      - 이로써 같은 봇이 1분 안에 N건 결제해도 stats.jsonl에는 N개 기록되되
        텔레그램 알림 노이즈는 억제

    network/payer/transaction: 미들웨어가 PAYMENT-RESPONSE 헤더 디코드 후 전달."""
    if endpoint not in PAID_ENDPOINTS_LIST or status_code != 200:
        return

    price_map = {
        "/api/v1/kimchi-premium": "$0.002",
        "/api/v1/kr-prices": "$0.002",
        "/api/v1/stablecoin-premium": "$0.002",
        "/api/v1/market-read": "$0.10",
        "/api/v1/kr-sentiment": "$0.05",
        "/api/v1/arbitrage-scanner": "$0.01",
        "/api/v1/exchange-alerts": "$0.01",
        "/api/v1/market-movers": "$0.01",
        "/api/v1/global-vs-korea-divergence": "$0.05",
        "/api/v1/global-vs-korea-divergence-deep": "$0.10",
        "/api/v1/kr-news/kpop": "$0.01",
        "/api/v1/kr-news/kpop-summary": "$0.05",
        "/api/v1/kr-news/semiconductor": "$0.02",
        "/api/v1/kr-news/semiconductor-summary": "$0.10",
        "/api/v1/krw-macro-stress": "$0.05",
        # XRPL variants — 1:1 mirror of EVM prices
        "/api/v1/xrpl/kimchi-premium": "$0.002",
        "/api/v1/xrpl/kr-prices": "$0.002",
        "/api/v1/xrpl/stablecoin-premium": "$0.002",
        "/api/v1/xrpl/arbitrage-scanner": "$0.01",
        "/api/v1/xrpl/exchange-alerts": "$0.01",
        "/api/v1/xrpl/market-movers": "$0.01",
        "/api/v1/xrpl/kr-news/kpop": "$0.01",
        "/api/v1/xrpl/kr-news/semiconductor": "$0.02",
        "/api/v1/xrpl/kr-sentiment": "$0.05",
        "/api/v1/xrpl/global-vs-korea-divergence": "$0.05",
        "/api/v1/xrpl/kr-news/kpop-summary": "$0.05",
        "/api/v1/xrpl/global-vs-korea-divergence-deep": "$0.10",
        "/api/v1/xrpl/market-read": "$0.10",
        "/api/v1/xrpl/kr-news/semiconductor-summary": "$0.10",
        "/api/v1/xrpl/krw-macro-stress": "$0.05",
        # fx-rate XRPL variant falls through to default "$0.001" like EVM fx-rate
    }
    price = price_map.get(endpoint, "$0.001")  # fx-rate falls through here at $0.001
    price_value = float(price.replace("$", ""))
    net_label = _network_label(network)

    # === payment_settled 이벤트는 매 settle 마다 무조건 기록 (dedupe 없음) ===
    try:
        settled_event = {
            "endpoint": endpoint.replace("/api/v1/", ""),
            "ip": ip,
            "network": network or "unknown",
            "network_label": net_label,
            "payer": payer or "unknown",
            "price_usd": price_value,
        }
        if transaction:
            settled_event["transaction"] = transaction
        log_event("payment_settled", **settled_event)
    except Exception as e:
        print(f"[STATS] payment_settled log failed: {e}")

    # === 텔레그램 알림 발송에만 5분 dedupe 적용 ===
    now = time.time()
    expired = [k for k, ts in _payment_alert_cache.items() if now - ts > 300]
    for k in expired:
        del _payment_alert_cache[k]
    dedupe_key = f"{endpoint}:{ip}:{network or 'na'}:{payer or 'na'}"
    if dedupe_key in _payment_alert_cache:
        return  # 알림만 억제 (위 payment_settled 는 이미 기록됨)
    _payment_alert_cache[dedupe_key] = now

    # === 알림 본문 작성 ===
    # IP 분류 (owner / residential / datacenter / unknown)
    is_owner = ip in _OWNER_IPS
    if is_owner:
        ip_type_emoji = "👤"
        ip_info_text = f"{ip} {ip_type_emoji} owner (본인 검증)"
    else:
        ip_info = await classify_ip(ip)
        ip_type_emoji = "🏢" if ip_info["type"] == "datacenter" else ("🏠" if ip_info["type"] == "residential" else "❓")
        ip_info_text = f"{ip} {ip_type_emoji} {ip_info['type']} ({ip_info['city']}, {ip_info['country']})"

    # 일일 카운트 + 매출 업데이트
    _daily_user_calls[ip] += 1
    _daily_user_revenue[ip] += price_value
    today_count = _daily_user_calls[ip]

    # 누적 데이터
    history = get_user_history(ip)
    total_count = history["total_calls"] + 1
    total_revenue_cum = history["total_revenue"] + price_value

    if today_count == 1:
        today_status = "🆕 오늘 첫 결제"
    elif today_count >= 5:
        today_status = f"🔥 활성 (오늘 {today_count}건째)"
    elif today_count >= 3:
        today_status = f"⭐ 정기 사용 (오늘 {today_count}건째)"
    else:
        today_status = f"📊 평가 중 (오늘 {today_count}건째)"

    if history["days_ago"] is not None and history["days_ago"] > 0:
        first_seen_text = f"첫 결제: {history['first_seen_date']} ({history['days_ago']}일 전)"
    else:
        first_seen_text = "첫 결제: 오늘"

    total_text = f"누적: {total_count}건 / ${total_revenue_cum:.4f}"

    high_value_emoji = ""
    if price_value >= 0.10:
        high_value_emoji = "💎 "
    elif price_value >= 0.05:
        high_value_emoji = "✨ "

    # 네트워크 + 지갑 라인 (Solana 등 IP 외 식별자 표시)
    network_line = f"네트워크: {net_label}"
    wallet_line = ""
    if payer:
        short = payer[:10] + "..." + payer[-8:] if len(payer) > 20 else payer
        wallet_line = f"\n지갑: {short}"

    await tg_send(
        f"{high_value_emoji}💰 유료 결제 성공!\n"
        f"엔드포인트: {endpoint}\n"
        f"가격: {price}\n"
        f"{network_line}{wallet_line}\n"
        f"IP: {ip_info_text}\n"
        f"사용자: {today_status}\n"
        f"{total_text}\n"
        f"{first_seen_text}\n"
        f"시간: {time.strftime('%H:%M:%S')}"
    )
    # 다음 결제 알림 시 fresh 누적 데이터 보장
    _user_history_cache.pop(ip, None)

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

def _count_xrpl_paid_api_calls(start_ts: int, end_ts: int) -> int:
    """Count api_call events with endpoint prefix 'xrpl/' in [start_ts, end_ts).
    Used to exclude XRPL settlements from the CDP facilitator fee calculation —
    XRPL settles via t54 facilitator, so CDP does not charge for those calls."""
    import json as _json
    from stats_logger import STATS_FILE
    n = 0
    try:
        with open(STATS_FILE) as f:
            for line in f:
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if (e.get("type") == "api_call"
                    and e.get("paid")
                    and start_ts <= e.get("ts", 0) < end_ts
                    and str(e.get("endpoint", "")).startswith("xrpl/")):
                    n += 1
    except FileNotFoundError:
        pass
    except Exception as ex:
        print(f"[CDP-XRPL-COUNT] read err: {ex}")
    return n


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

            # 전일 KST 00:00~24:00 구간 집계.
            # 주의: next_9am 은 sleep 전에 계산된 값이라 NTP 보정/oversleep/클럭 점프 시
            # 실제 wakeup 시각과 어긋날 수 있음. sleep 직후의 현재 KST 시각을 직접 사용.
            current_kst = datetime.now(timezone(timedelta(hours=9)))
            today_midnight_kst = current_kst.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_midnight_kst = today_midnight_kst - timedelta(days=1)
            yesterday_start = int(yesterday_midnight_kst.timestamp())
            yesterday_end = int(today_midnight_kst.timestamp())
            s = aggregate_stats_range(yesterday_start, yesterday_end)
            date_str = yesterday_midnight_kst.strftime("%Y-%m-%d")

            # === CDP facilitator fee (Base/Polygon/Solana settle cost) ===
            # Coinbase 정책: 월 1000건 무료 + 초과 $0.001/건. 어제가 무료 한도
            # 경계를 걸친 날일 수도 있어 [월초~어제끝] - [월초~어제시작] 델타로 계산.
            # XRPL 결제는 t54 facilitator 사용 → CDP 수수료 부과 대상 아님 → 카운트에서 제외.
            month_start_kst = yesterday_midnight_kst.replace(day=1)
            month_start_ts = int(month_start_kst.timestamp())
            month_through_yesterday = aggregate_stats_range(month_start_ts, yesterday_end)
            month_before_yesterday = aggregate_stats_range(month_start_ts, yesterday_start)
            # Subtract XRPL api_call events — they don't go through CDP.
            month_xrpl_through = _count_xrpl_paid_api_calls(month_start_ts, yesterday_end)
            month_xrpl_before = _count_xrpl_paid_api_calls(month_start_ts, yesterday_start)
            month_cdp_calls = max(0, month_through_yesterday["paid_calls"] - month_xrpl_through)
            month_cdp_calls_before = max(0, month_before_yesterday["paid_calls"] - month_xrpl_before)
            cdp_at_end, cdp_used, cdp_remaining = calculate_cdp_cost(month_cdp_calls)
            cdp_before, _, _ = calculate_cdp_cost(month_cdp_calls_before)
            today_cdp_cost = max(0.0, cdp_at_end - cdp_before)
            month_paid_calls = month_through_yesterday["paid_calls"]  # kept for report totals
            month_cdp_excess = max(0, month_cdp_calls - 1000)          # excess against CDP-eligible calls only

            profit = s["revenue_usd"] - s["claude_cost_usd"] - today_cdp_cost

            # 사용자 활동 분류 (in-memory _daily_user_* 기준 — KST 자정 리셋 직후 호출되므로
            # 이 시점의 데이터는 막 끝난 KST 일자의 활동을 반영)
            total_revenue_user = sum(_daily_user_revenue.values())
            total_calls_user = sum(_daily_user_calls.values())

            owner_users = []          # 본인(Moa) 검증 결제 — 진성 카운트에서 제외
            residential_users = []
            datacenter_users = []
            unknown_users = []
            for ip, calls in _daily_user_calls.items():
                revenue = _daily_user_revenue[ip]
                if ip in _OWNER_IPS:
                    owner_users.append((ip, calls, revenue))
                    continue
                info = await classify_ip(ip)
                entry = (ip, calls, revenue, info)
                if info["type"] == "residential":
                    residential_users.append(entry)
                elif info["type"] == "datacenter":
                    datacenter_users.append(entry)
                else:
                    unknown_users.append(entry)

            owner_users.sort(key=lambda x: -x[2])
            residential_users.sort(key=lambda x: -x[2])
            datacenter_users.sort(key=lambda x: -x[2])

            xrpl_note = f" — XRPL {month_xrpl_through:,}건 제외" if month_xrpl_through > 0 else ""
            msg = (
                f"📊 <b>일일 리포트</b> — {date_str}\n\n"
                f"API 호출: {s['api_calls_total']}건 (HIT {s['cache_hits']}, MISS {s['api_calls_total'] - s['cache_hits']})\n"
                f"유료 결제: {s['paid_calls']}건 (${s['revenue_usd']:.4f})\n"
                f"김프 알림: {s['alerts_sent']}건\n"
                f"Claude 비용: ${s['claude_cost_usd']:.4f}\n"
                f"CDP 수수료: ${today_cdp_cost:.4f}\n"
                f"에러: {s['errors']}건\n\n"
                f"💰 일 순이익: ${profit:.4f}\n"
                f"📅 당월 CDP: {month_cdp_calls:,}/1,000건 ({month_cdp_excess:,}건 초과, ${cdp_at_end:.4f}){xrpl_note}\n"
                f"{'─' * 25}\n\n"
                f"👥 <b>사용자 활동</b> (in-memory, {total_calls_user}건 / ${total_revenue_user:.4f})\n"
            )

            if owner_users:
                owner_total = sum(u[2] for u in owner_users)
                owner_calls = sum(u[1] for u in owner_users)
                msg += f"\n👤 본인 검증 결제: {owner_calls}건 (${owner_total:.4f}) — 진성 카운트에서 제외\n"

            if residential_users:
                msg += "\n🏠 가정 ISP 사용자 (진성):\n"
                for ip, calls, revenue, info in residential_users[:5]:
                    msg += f"• {info['city']}, {info['country']} — {calls}건 (${revenue:.4f})\n"

            if datacenter_users:
                msg += "\n🏢 데이터센터 사용자 (봇/AI 에이전트):\n"
                for ip, calls, revenue, info in datacenter_users[:5]:
                    org = info['org_short'][:20]
                    msg += f"• {info['city']}, {info['country']} ({org}) — {calls}건 (${revenue:.4f})\n"

            if unknown_users:
                msg += f"\n❓ 분류 불가: {len(unknown_users)}명\n"

            # 진성 사용자 카운트: owner 제외, datacenter는 3건 이상만 진성으로 가산
            real_user_count = len(residential_users) + len([d for d in datacenter_users if d[1] >= 3])
            msg += f"\n⭐ 추정 진성 사용자: {real_user_count}명 (본인 제외)"

            # Post-settle failure summary (settle 성공 but 5xx 응답)
            try:
                ps_agg = _aggregate_post_settle_failures(yesterday_start, yesterday_end)
                msg += "\n" + _render_post_settle_summary_lines(ps_agg)
            except Exception as _ps_err:
                print(f"[DAILY] post_settle_failure aggregation error: {_ps_err}")

            # MCP call summary (Tier 1-6 breakdown + new clients)
            try:
                mcp_agg = _aggregate_mcp_calls(yesterday_start, yesterday_end)
                msg += "\n\n" + _render_mcp_summary_lines(mcp_agg)
            except Exception as _mcp_err:
                print(f"[DAILY] mcp aggregation error: {_mcp_err}")

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
    # 일일 사용자 활동 카운터 KST 자정 리셋
    task5 = asyncio.create_task(daily_user_stats_reset())
    yield
    task1.cancel()
    task2.cancel()
    task3.cancel()
    task4.cancel()
    task5.cancel()
    save_stats()

# === x402 결제 설정 ===
WALLET_ADDRESS = "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"
SOLANA_WALLET = "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
POLYGON_NETWORK = "eip155:137"
FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

# === XRPL merchant setup (Path C — /api/v1/xrpl/* isolated routes) ===
# payTo address is public (safe in env). Seed never on server — merchant only
# receives, doesn't sign. RLUSD issuer is fixed on mainnet (on-chain verified:
# Domain=https://ripple.com/, issues currency 524C555344...), kept as a constant
# rather than env so an accidental env misconfiguration cannot silently misroute
# funds to a wrong issuer.
XRPL_MERCHANT_ADDR = os.getenv("XRPL_PAY_TO", "").strip()
XRPL_FACILITATOR_URL = os.getenv("XRPL_FACILITATOR_URL", "https://xrpl-facilitator-mainnet.t54.ai")
XRPL_NETWORK = os.getenv("XRPL_NETWORK", "xrpl:0")   # mainnet
XRPL_RLUSD_ISSUER_MAINNET = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"   # Ripple, verified on-chain
XRPL_RLUSD_HEX = "524C555344000000000000000000000000000000"
XRPL_SOURCE_TAG = 804681468   # x402watch indexing standard
# Path C XRPL variants grouped by price. Each require_payment instance
# advertises a single (amount, asset, pay_to, network) — the SDK does not
# support per-path pricing inside one middleware, so we register one
# middleware per price bucket (6 total, covering all 15 XRPL routes).
XRPL_PRICE_GROUPS = {
    "0.001": ["/api/v1/xrpl/fx-rate"],
    "0.002": ["/api/v1/xrpl/kimchi-premium", "/api/v1/xrpl/kr-prices", "/api/v1/xrpl/stablecoin-premium"],
    "0.01":  ["/api/v1/xrpl/arbitrage-scanner", "/api/v1/xrpl/exchange-alerts",
              "/api/v1/xrpl/market-movers", "/api/v1/xrpl/kr-news/kpop"],
    "0.02":  ["/api/v1/xrpl/kr-news/semiconductor"],
    "0.05":  ["/api/v1/xrpl/kr-sentiment", "/api/v1/xrpl/global-vs-korea-divergence",
              "/api/v1/xrpl/kr-news/kpop-summary", "/api/v1/xrpl/krw-macro-stress"],
    "0.10":  ["/api/v1/xrpl/global-vs-korea-divergence-deep", "/api/v1/xrpl/market-read",
              "/api/v1/xrpl/kr-news/semiconductor-summary"],
}
# Flat list for use in PAID_ENDPOINTS_LIST-adjacent audit/regression checks.
XRPL_PROTECTED_PATHS = [p for paths in XRPL_PRICE_GROUPS.values() for p in paths]

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


# Catalog metadata per endpoint — search relevance enrichment for CDP Bazaar
# discovery (Cryptorefills catalog-discovery-at-scale playbook).
# Mutates the extensions dict returned by declare_discovery_extension() in-place.
_CATALOG_COMMON = {
    "locale": "ko-KR",
    "jurisdiction": "KR",
    "target_users": ["ai-agents", "trading-bots"],
    "language": "en",
}
_CATALOG_PER_ENDPOINT = {
    "/api/v1/kimchi-premium": {"data_sources": ["upbit", "binance", "fx"], "update_frequency": "real-time", "category": "korean-crypto-data"},
    "/api/v1/kr-prices": {"data_sources": ["upbit", "bithumb"], "update_frequency": "real-time", "category": "korean-crypto-data"},
    "/api/v1/fx-rate": {"data_sources": ["exchangerate-api"], "update_frequency": "hourly", "category": "exchange-rate"},
    "/api/v1/stablecoin-premium": {"data_sources": ["upbit", "bithumb"], "update_frequency": "real-time", "category": "korean-crypto-data"},
    "/api/v1/arbitrage-scanner": {"data_sources": ["upbit", "bithumb", "binance"], "update_frequency": "real-time", "category": "korean-crypto-data"},
    "/api/v1/exchange-alerts": {"data_sources": ["upbit", "bithumb"], "update_frequency": "event-driven", "category": "korean-crypto-data"},
    "/api/v1/market-movers": {"data_sources": ["upbit", "bithumb"], "update_frequency": "1-min", "category": "korean-crypto-data"},
    "/api/v1/kr-sentiment": {"data_sources": ["coinness", "korean-news", "claude-ai"], "update_frequency": "5-min", "category": "korean-crypto-sentiment"},
    "/api/v1/global-vs-korea-divergence": {"data_sources": ["upbit", "binance", "claude-ai"], "update_frequency": "real-time", "category": "korean-crypto-data"},
    "/api/v1/global-vs-korea-divergence-deep": {"data_sources": ["upbit", "binance", "coinness", "claude-ai"], "update_frequency": "real-time", "category": "korean-crypto-data"},
    "/api/v1/market-read": {"data_sources": ["upbit", "bithumb", "binance", "fx", "claude-ai", "exchange-intel"], "update_frequency": "5-min", "category": "korean-crypto-analysis"},
    "/api/v1/kr-news/kpop": {"data_sources": ["naver-news", "claude-haiku"], "update_frequency": "5-min", "category": "korean-news"},
    "/api/v1/kr-news/kpop-summary": {"data_sources": ["naver-news", "claude-haiku"], "update_frequency": "5-min", "category": "korean-news-analysis"},
    "/api/v1/kr-news/semiconductor": {"data_sources": ["naver-news", "claude-haiku"], "update_frequency": "5-min", "category": "korean-news"},
    "/api/v1/kr-news/semiconductor-summary": {"data_sources": ["naver-news", "claude-haiku"], "update_frequency": "5-min", "category": "korean-news-analysis"},
    "/api/v1/krw-macro-stress": {"data_sources": ["fred", "yfinance", "naver-finance", "claude-haiku"], "update_frequency": "15-min", "category": "korean-macro-signal", "locale": "ko-KR", "jurisdiction": "KR"},
    "/api/v1/xrpl/krw-macro-stress": {"data_sources": ["fred", "yfinance", "naver-finance", "claude-haiku"], "update_frequency": "15-min", "category": "korean-macro-signal", "locale": "ko-KR", "jurisdiction": "KR"},
}


def _with_catalog(endpoint_path: str, extensions: dict) -> dict:
    """Merge catalog metadata into an extensions dict (returned by declare_discovery_extension).
    Mutates and returns the same dict so it remains a single RouteConfig kwarg."""
    per_ep = _CATALOG_PER_ENDPOINT.get(endpoint_path, {})
    extensions["catalog"] = {**_CATALOG_COMMON, **per_ep}
    return extensions

x402_routes = {
    "GET /api/v1/kimchi-premium": RouteConfig(
        accepts=_pay_opts("$0.002"),
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
                    "fx_rate_official": 1475.27,
                    "usdt_krw_rate": 1480.5,
                    "fx_source": "exchangerate-api.com",
                    "binance_krw_equivalent": 140453370,
                    "premium_percent": 1.1,
                    "premium_pct_usdt": 0.78,
                    "premium_direction": "positive",
                    "timestamp": 1776340000000,
                }
            ),
        ),
    ),
    "GET /api/v1/kr-prices": RouteConfig(
        accepts=_pay_opts("$0.002"),
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
        accepts=_pay_opts("$0.002"),
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
    "GET /api/v1/krw-macro-stress": RouteConfig(
        accepts=_pay_opts("$0.05"),
        description="KRW Macro Stress Score (0-100) — combined signal from US 3Y treasury, VIX, foreign ownership proxy (SK Hynix + Samsung, mcap-weighted), USD/KRW momentum, and Korean semiconductor equity. Rolling 120d percentile over 2yr backfill. Regime + direction + per-component breakdown. Positioning: KRW macro stress, not kimchi-premium prediction.",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            output=OutputConfig(
                example={
                    "score": 45.4,
                    "regime": "caution",
                    "direction": "krw_stable",
                    "components": {
                        "us_rate_stress": {"score": 94.17, "raw": {"us_3y_yield_pct": 4.34}, "freshness": "0d"},
                        "risk_sentiment": {"score": 6.67, "raw": {"vix": 15.86}, "freshness": "0d"},
                        "foreign_flow": {"score": 28.33, "raw": {"mcap_weighted_foreign_pct": 48.576, "delta_5d_pp": 0.045, "note": "(proxy: SK Hynix + Samsung mcap-weighted foreign ownership %; not direct netbuy amount)"}, "freshness": "0d"},
                        "fx_momentum": {"score": 2.5, "raw": {"usdkrw": 1425.48, "pct_change_5d": -2.66}, "freshness": "0d"},
                        "semiconductor": {"score": 48.33, "raw": {"sk_hynix_krw": 1535000.0, "samsung_krw": 235250.0}, "freshness": "0d"},
                    },
                    "ai_note": "Score 45 sits in the caution band. VIX percentile is low while US 3Y sits near a 120d high; foreign ownership drift is muted.",
                    "market_hours": {"krx": "closed", "us": "closed"},
                    "as_of": "2026-08-04T02:37:05Z",
                    "method": "rolling percentile 120d, weights 25/25/20/20/10",
                    "degraded": [],
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
    # === Korean news → English (4 endpoints) ===
    "GET /api/v1/kr-news/kpop": RouteConfig(
        accepts=_pay_opts("$0.01"),
        description="Korean K-pop news translated to English. Naver-aggregated headlines from Korean media (Yonhap, Korea Economic Daily, Chosun Ilbo, etc.) auto-classified for K-pop relevance and translated by Claude. Returns title_en + summary_en + source_en + original Korean references. 5-min cache. limit=1..10.",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"limit": 5},
            input_schema={
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Number of articles to return (default 5)"},
                },
            },
            output=OutputConfig(
                example={
                    "ok": True,
                    "category": "kpop",
                    "results": [
                        {
                            "title_kr": "BTS, 군 전역 후 첫 단체 활동 시작",
                            "title_en": "BTS launches first group activity after military discharge",
                            "summary_en": "All seven members reunite for an upcoming album release scheduled for late May, the first full-group project since 2022.",
                            "source_kr": "한국경제",
                            "source_en": "Korea Economic Daily",
                            "published_at": "2026-05-11T10:00:00+09:00",
                            "link": "https://hankyung.com/article/...",
                        }
                    ],
                    "count": 5,
                    "timestamp": "2026-05-11T10:05:00Z",
                    "_meta": {"cache_age_seconds": 0},
                }
            ),
        ),
    ),
    "GET /api/v1/kr-news/kpop-summary": RouteConfig(
        accepts=_pay_opts("$0.05"),
        description="Korean K-pop news with AI synthesis. Returns the headline list (English-translated) PLUS a Sonnet 4.6 analysis: overall_sentiment, key_themes, trending_entities, and a paragraph summary. For agents tracking Korean entertainment industry pulse. 5-min cache.",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"limit": 5},
            input_schema={
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Number of articles to analyze (default 5)"},
                },
            },
            output=OutputConfig(
                example={
                    "ok": True,
                    "category": "kpop",
                    "results": [
                        {
                            "title_kr": "BTS, 군 전역 후 첫 단체 활동 시작",
                            "title_en": "BTS launches first group activity after military discharge",
                            "summary_en": "All seven members reunite for an upcoming album release.",
                            "source_kr": "한국경제",
                            "source_en": "Korea Economic Daily",
                            "published_at": "2026-05-11T10:00:00+09:00",
                            "link": "https://hankyung.com/...",
                        }
                    ],
                    "count": 5,
                    "ai_analysis": {
                        "overall_sentiment": "positive",
                        "key_themes": ["BTS reunion", "4th-gen group expansion", "Japanese market activity"],
                        "trending_entities": ["BTS", "NewJeans", "TWS", "ILLIT"],
                        "summary_en": "Korean K-pop industry shows strong positive momentum centered on BTS' full-group return and rising 4th-generation acts.",
                    },
                    "timestamp": "2026-05-11T10:05:00Z",
                }
            ),
        ),
    ),
    "GET /api/v1/kr-news/semiconductor": RouteConfig(
        accepts=_pay_opts("$0.02"),
        description="Korean semiconductor industry news translated to English. Covers Samsung Electronics, SK Hynix, HBM/DRAM/NAND memory products, foundry, and supplier ecosystem. Headlines from Korean tech press (전자신문/디지털타임스/한국경제) auto-classified and translated by Claude. 5-min cache.",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"limit": 5},
            input_schema={
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Number of articles to return (default 5)"},
                },
            },
            output=OutputConfig(
                example={
                    "ok": True,
                    "category": "semiconductor",
                    "results": [
                        {
                            "title_kr": "삼성전자, HBM4 양산 시작…NVIDIA 인증 통과",
                            "title_en": "Samsung Electronics begins HBM4 mass production after NVIDIA qualification",
                            "summary_en": "Korea's largest chipmaker started HBM4 12-Hi mass production at Pyeongtaek fab and shipped first samples to NVIDIA for the next-gen AI accelerator.",
                            "source_kr": "전자신문",
                            "source_en": "Electronic Times News",
                            "published_at": "2026-05-11T08:00:00+09:00",
                            "link": "https://etnews.com/...",
                        }
                    ],
                    "count": 5,
                    "timestamp": "2026-05-11T10:05:00Z",
                    "_meta": {"cache_age_seconds": 0},
                }
            ),
        ),
    ),
    "GET /api/v1/kr-news/semiconductor-summary": RouteConfig(
        accepts=_pay_opts("$0.10"),
        description="Korean semiconductor industry news with AI market synthesis. Returns headlines (English-translated) PLUS Sonnet 4.6 analysis: overall_sentiment, key_themes, trending_entities, market_signal (bullish/bearish/neutral), and a paragraph synthesis. For agents tracking Korean memory/foundry industry signals. 5-min cache.",
        mime_type="application/json",
        extensions=declare_discovery_extension(
            input={"limit": 5},
            input_schema={
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Number of articles to analyze (default 5)"},
                },
            },
            output=OutputConfig(
                example={
                    "ok": True,
                    "category": "semiconductor",
                    "results": [
                        {
                            "title_kr": "삼성전자, HBM4 양산 시작",
                            "title_en": "Samsung Electronics begins HBM4 mass production",
                            "summary_en": "Korea's largest chipmaker started HBM4 mass production at Pyeongtaek.",
                            "source_kr": "전자신문",
                            "source_en": "Electronic Times News",
                            "published_at": "2026-05-11T08:00:00+09:00",
                            "link": "https://etnews.com/...",
                        }
                    ],
                    "count": 5,
                    "ai_analysis": {
                        "overall_sentiment": "positive",
                        "key_themes": ["HBM4 mass production", "AI accelerator demand", "memory supercycle"],
                        "trending_entities": ["Samsung Electronics", "SK Hynix", "HBM4", "NVIDIA"],
                        "market_signal": "bullish",
                        "summary_en": "Korean memory makers are entering a structural HBM upcycle as Samsung clears NVIDIA HBM4 qualification, narrowing SK Hynix's lead and confirming the AI-driven memory supercycle.",
                    },
                    "timestamp": "2026-05-11T10:05:00Z",
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

# x402watch merchant feed (Phase 2c)
from x402watch_feed import router as x402watch_feed_router
app.include_router(x402watch_feed_router)


# === AgentCash discovery — OpenAPI metadata overlay =========================
# Adds x-payment-info / x-guidance / x-discovery so AgentCash search() can
# recognise paid routes + auth modes. Pure additive metadata — x402 clients
# ignore unknown OpenAPI fields, so existing flows are unaffected.
from fastapi.openapi.utils import get_openapi as _agentcash_get_openapi

PAID_ENDPOINTS_PRICING = {
    "/api/v1/kimchi-premium": "0.002000",
    "/api/v1/kr-prices": "0.002000",
    "/api/v1/fx-rate": "0.001000",
    "/api/v1/stablecoin-premium": "0.002000",
    "/api/v1/arbitrage-scanner": "0.010000",
    "/api/v1/exchange-alerts": "0.010000",
    "/api/v1/market-movers": "0.010000",
    "/api/v1/kr-news/kpop": "0.010000",
    "/api/v1/kr-news/semiconductor": "0.020000",
    "/api/v1/global-vs-korea-divergence": "0.050000",
    "/api/v1/kr-sentiment": "0.050000",
    "/api/v1/kr-news/kpop-summary": "0.050000",
    "/api/v1/global-vs-korea-divergence-deep": "0.100000",
    "/api/v1/market-read": "0.100000",
    "/api/v1/kr-news/semiconductor-summary": "0.100000",
    "/api/v1/krw-macro-stress": "0.050000",
    # XRPL variants — 6-decimal string mirror of EVM PAID_ENDPOINTS_PRICING
    "/api/v1/xrpl/kimchi-premium": "0.002000",
    "/api/v1/xrpl/kr-prices": "0.002000",
    "/api/v1/xrpl/fx-rate": "0.001000",
    "/api/v1/xrpl/stablecoin-premium": "0.002000",
    "/api/v1/xrpl/arbitrage-scanner": "0.010000",
    "/api/v1/xrpl/exchange-alerts": "0.010000",
    "/api/v1/xrpl/market-movers": "0.010000",
    "/api/v1/xrpl/kr-news/kpop": "0.010000",
    "/api/v1/xrpl/kr-news/semiconductor": "0.020000",
    "/api/v1/xrpl/global-vs-korea-divergence": "0.050000",
    "/api/v1/xrpl/kr-sentiment": "0.050000",
    "/api/v1/xrpl/kr-news/kpop-summary": "0.050000",
    "/api/v1/xrpl/global-vs-korea-divergence-deep": "0.100000",
    "/api/v1/xrpl/market-read": "0.100000",
    "/api/v1/xrpl/kr-news/semiconductor-summary": "0.100000",
    "/api/v1/xrpl/krw-macro-stress": "0.050000",
}

FREE_ENDPOINTS = [
    "/",
    "/.well-known/x402",
    "/llms.txt",
    "/health",
    "/api/v1/symbols",
    "/api/v1/stats",
]


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = _agentcash_get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    openapi_schema["info"]["x-guidance"] = (
        "Korean crypto market + news data API for AI agents. "
        "Use GET /api/v1/kimchi-premium for single-token Kimchi Premium. "
        "Use GET /api/v1/kr-sentiment for Korean market sentiment in English. "
        "Use GET /api/v1/kr-news/kpop for K-pop news translated to English. "
        "Use GET /api/v1/kr-news/semiconductor for Korean semiconductor industry news. "
        "All paid endpoints require x402 payment ($0.001-$0.10 in USDC or RLUSD). "
        "Networks: Base, Polygon, Solana (USDC), XRPL (RLUSD via /api/v1/xrpl/<endpoint>). "
        "Every paid response includes signed receipt (ECDSA secp256k1) for agent accountability."
    )

    openapi_schema["x-discovery"] = {"ownershipProofs": []}

    # Paid endpoints — attach x-payment-info + 402 response shape
    for _path, _amount in PAID_ENDPOINTS_PRICING.items():
        if _path not in openapi_schema["paths"]:
            continue
        for _method, _op in openapi_schema["paths"][_path].items():
            if _method.lower() != "get" or not isinstance(_op, dict):
                continue
            _op["x-payment-info"] = {
                "price": {"mode": "fixed", "currency": "USD", "amount": _amount},
                "protocols": [{"x402": {}}],
            }
            _op.setdefault("responses", {})["402"] = {
                "description": "Payment Required",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "accepts": {"type": "array"},
                                "x402Version": {"type": "integer"},
                            },
                        }
                    }
                },
            }

    # Free endpoints — explicit auth-mode declaration so AgentCash discovery
    # doesn't emit L2/L3_AUTH_MODE_MISSING. `security: []` alone is the OpenAPI
    # spec for "no auth required" but AgentCash wants an explicit x-auth-mode.
    for _path in FREE_ENDPOINTS:
        if _path not in openapi_schema["paths"]:
            continue
        for _method, _op in openapi_schema["paths"][_path].items():
            if _method.lower() in ("get", "post") and isinstance(_op, dict):
                _op["security"] = []
                _op["x-auth-mode"] = "none"

    app.openapi_schema = openapi_schema
    return openapi_schema


app.openapi = custom_openapi


# Inject catalog metadata into each route's extensions dict (in place, before
# middleware registration). This enriches CDP Bazaar search relevance without
# requiring per-route edits to 15 RouteConfig blocks.
for _route_key, _rc in x402_routes.items():
    try:
        _ep_path = _route_key.split(" ", 1)[1] if " " in _route_key else _route_key
        _ext = getattr(_rc, "extensions", None)
        if isinstance(_ext, dict):
            _with_catalog(_ep_path, _ext)
    except Exception as _e:
        print(f"[CATALOG] failed to attach catalog for {_route_key}: {_e}")

# x402 결제 미들웨어 적용
app.add_middleware(PaymentMiddlewareASGI, routes=x402_routes, server=x402_server)

# XRPL Path C middleware — isolated to /api/v1/xrpl/* only. Passes through
# every request that is not in the path set of a given require_payment call
# (SDK-internal path filter), so it never touches Base/Polygon/Solana flows.
# Watches PAYMENT-SIGNATURE header, which is orthogonal to X-PAYMENT used by
# PaymentMiddlewareASGI.
#
# One require_payment instance per price bucket — the SDK does not support
# per-path pricing inside a single middleware, and 15 endpoints span 6 prices
# ($0.001 / $0.002 / $0.01 / $0.02 / $0.05 / $0.10). Middleware order is
# irrelevant because each instance short-circuits on paths it doesn't own.
#
# Skipped entirely (no middleware wiring) when XRPL_MERCHANT_ADDR is empty,
# so a missing/misconfigured .env does not accidentally register any route.
if XRPL_MERCHANT_ADDR:
    for _xrpl_price, _xrpl_paths in XRPL_PRICE_GROUPS.items():
        app.middleware("http")(
            _xrpl_require_payment(
                path=_xrpl_paths,
                price=_xrpl_price,   # RLUSD (IOU) — decimal-value string
                pay_to_address=XRPL_MERCHANT_ADDR,
                facilitator_url=XRPL_FACILITATOR_URL,
                network=XRPL_NETWORK,
                asset=XRPL_RLUSD_HEX,
                issuer=XRPL_RLUSD_ISSUER_MAINNET,
                source_tag=XRPL_SOURCE_TAG,
                description=f"KR Crypto — XRPL / RLUSD ({_xrpl_price} RLUSD tier)",
                mime_type="application/json",
            )
        )
    print(f"[XRPL] require_payment registered for {len(XRPL_PROTECTED_PATHS)} paths "
          f"across {len(XRPL_PRICE_GROUPS)} price buckets → {XRPL_MERCHANT_ADDR}")
else:
    print("[XRPL] skipped: XRPL_PAY_TO not set in .env")

from starlette.responses import Response as _StarletteResponse


def _decode_payment_response_header(response) -> tuple:
    """Decode the x402 PAYMENT-RESPONSE header (base64 JSON SettleResponse).
    Returns (payer, transaction, network) — any may be None on failure or absence."""
    import base64 as _b64
    pr_header = (response.headers.get("PAYMENT-RESPONSE")
                 or response.headers.get("payment-response")
                 or response.headers.get("X-PAYMENT-RESPONSE")
                 or response.headers.get("x-payment-response"))
    if not pr_header:
        return (None, None, None)
    try:
        settle_json = json.loads(_b64.b64decode(pr_header))
        if isinstance(settle_json, dict):
            return (settle_json.get("payer"),
                    settle_json.get("transaction"),
                    settle_json.get("network"))
    except Exception as e:
        print(f"[x402-decode] PAYMENT-RESPONSE decode failed: {e}")
    return (None, None, None)


async def _buffer_response_body(response) -> bytes:
    # Starlette middleware wraps the inner response in _StreamingResponse which
    # exposes body_iterator; direct Response subclasses expose .body. Handle both.
    if hasattr(response, "body_iterator"):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)
    return getattr(response, "body", b"") or b""


def _strip_length_headers(headers):
    """Remove headers that Response init will recompute (content-length).
    Returns a list of (key, value) tuples preserving multi-valued PAYMENT-RESPONSE etc."""
    out = []
    for k, v in headers.raw:
        if k.lower() == b"content-length":
            continue
        out.append((k.decode("latin-1"), v.decode("latin-1")))
    return out


# === CORS headers (browser preflight + actual response) =====================
# x402 PaymentMiddlewareASGI 405s OPTIONS before CORSMiddleware can respond,
# so we short-circuit preflight here and add Allow-Origin / Expose-Headers
# on every response. allow_credentials must stay False with origin "*".
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "86400",
    "Access-Control-Expose-Headers": (
        "PAYMENT-RESPONSE, X-PAYMENT-RESPONSE, payment-response, x-payment-response, "
        "Payment-Required, payment-required"
    ),
}


def _apply_cors_headers(response):
    for k, v in _CORS_HEADERS.items():
        if k not in response.headers:
            response.headers[k] = v
    return response


def _patch_402_resource(response, endpoint: str, request_url: str):
    """Decode the base64 payment-required header, inject resource + extra.resource
    into each accept, re-encode. The 402 body is currently '{}' so we mirror the
    challenge into the body as well (validators that read body still see it).

    Returns a new Starlette Response with patched header + body. If anything
    fails, returns the original response untouched."""
    import base64 as _b64
    try:
        header_name = None
        pr_value = None
        for k in ("payment-required", "Payment-Required", "X-Payment-Required", "x-payment-required"):
            v = response.headers.get(k)
            if v:
                header_name = k
                pr_value = v
                break
        if not pr_value:
            return response
        challenge = json.loads(_b64.b64decode(pr_value))
        if not isinstance(challenge, dict):
            return response

        # Top-level resource URL preferred; fall back to the live request URL.
        resource_url = (
            (challenge.get("resource") or {}).get("url")
            if isinstance(challenge.get("resource"), dict)
            else None
        ) or request_url

        accepts = challenge.get("accepts") or []
        for a in accepts:
            if not isinstance(a, dict):
                continue
            a.setdefault("resource", resource_url)
            extra = a.get("extra")
            if not isinstance(extra, dict):
                extra = {}
                a["extra"] = extra
            extra.setdefault("resource", resource_url)
        challenge["accepts"] = accepts

        new_b64 = _b64.b64encode(json.dumps(challenge, ensure_ascii=False).encode()).decode()

        # Rebuild response with patched header + body (body was previously '{}').
        new_body = json.dumps(challenge, ensure_ascii=False).encode("utf-8")
        headers = _strip_length_headers(response.headers)
        new_headers = dict(headers)
        # Replace the header (whatever casing the original used)
        for k in list(new_headers.keys()):
            if k.lower() in ("payment-required", "x-payment-required"):
                del new_headers[k]
        new_headers["payment-required"] = new_b64
        # CORS expose includes both names
        return _StarletteResponse(
            content=new_body,
            status_code=402,
            headers=new_headers,
            media_type="application/json",
        )
    except Exception as e:
        print(f"[x402-402-patch] failed for {endpoint}: {e}")
        return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # === CORS preflight short-circuit (must precede x402's 405) =============
    if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        return _StarletteResponse(status_code=204, headers=_CORS_HEADERS)

    if request.url.path in ("/health", "/docs", "/openapi.json", "/", "/favicon.ico", "/llms.txt", "/.well-known/x402"):
        return _apply_cors_headers(await call_next(request))
    ip = get_real_ip(request)
    # x402 discovery probes: unpaid GETs to paid endpoints receive the cheap,
    # static 402 challenge without consuming rate-limit quota. Requests carrying
    # X-PAYMENT (EVM/SVM) or PAYMENT-SIGNATURE (XRPL/t54) — i.e. actual
    # settlement attempts — remain rate-limited.
    x402_probe = (request.method == "GET"
                  and request.url.path in PAID_ENDPOINTS_LIST
                  and "x-payment" not in request.headers
                  and "payment-signature" not in request.headers)
    if not x402_probe and not check_rate_limit(ip):
        return _apply_cors_headers(JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Max 60 requests per minute.", "retry_after_seconds": 60}))
    response = await call_next(request)
    endpoint = request.url.path

    # Patch 402 challenge (paid endpoint, no X-PAYMENT) to repeat resource URL
    if response.status_code == 402 and endpoint in PAID_ENDPOINTS_LIST:
        response = _patch_402_resource(response, endpoint, str(request.url))

    # Fast path: non-/api/v1/ routes (e.g. /llms.txt was already excluded above)
    if not endpoint.startswith("/api/v1/"):
        return _apply_cors_headers(response)

    try:
        symbol = request.query_params.get("symbol", "")

        # 1) network from request.state (set by x402 verify step). Free calls
        #    have no attr → graceful.
        network = None
        try:
            pr = getattr(request.state, "payment_requirements", None)
            if pr is not None:
                network = getattr(pr, "network", None)
        except Exception:
            pass

        # 2) payer/transaction: PAYMENT-RESPONSE header (base64 SettleResponse)
        payer, transaction, network_from_settle = _decode_payment_response_header(response)
        if not network:
            network = network_from_settle

        is_paid_endpoint = endpoint in PAID_ENDPOINTS_LIST
        settled = bool(transaction or payer)  # PAYMENT-RESPONSE present + parsed

        # === Receipt injection (200 + settled) ===============================
        if is_paid_endpoint and settled and response.status_code == 200:
            try:
                body = await _buffer_response_body(response)
                try:
                    data = json.loads(body)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    # Per-chain merchant address + currency dispatch.
                    #  - XRPL   → XRPL_MERCHANT_ADDR (r...) + RLUSD
                    #  - Solana → SOLANA_WALLET (3Ywxk…)   + USDC
                    #  - EVM (Base/Polygon) → WALLET_ADDRESS (0xcF92…) + USDC
                    # network comes from PAYMENT-RESPONSE header (CAIP-2 full
                    # form: "solana:...", "eip155:8453", "xrpl:0") — same
                    # source that populates network_label in stats.jsonl.
                    _is_xrpl = endpoint.startswith("/api/v1/xrpl/")
                    _net_str = str(network or "")
                    if _is_xrpl and XRPL_MERCHANT_ADDR:
                        _merchant_for_receipt = XRPL_MERCHANT_ADDR
                    elif _net_str.startswith("solana:"):
                        _merchant_for_receipt = SOLANA_WALLET
                    else:
                        _merchant_for_receipt = WALLET_ADDRESS
                    _currency_for_receipt = "RLUSD" if _is_xrpl else "USDC"
                    try:
                        data["receipt"] = _create_receipt(
                            endpoint=endpoint,
                            network=network or "unknown",
                            tx_hash=transaction or "",
                            payer=payer or "",
                            merchant=_merchant_for_receipt,
                            currency=_currency_for_receipt,
                        )
                    except Exception as e:
                        print(f"[RECEIPT] generation failed for {endpoint}: {e}")
                        data.setdefault("_meta", {})["receipt_status"] = "generation_failed"
                        # Alert (5-min dedupe) — non-blocking
                        price = float(str(_ENDPOINT_PRICES.get(endpoint, "0.001")))
                        asyncio.create_task(_send_post_settle_alert(
                            "receipt_failed", endpoint, ip,
                            payer or "", transaction or "",
                            price, 200, error_summary=str(e)[:200],
                            tg_send=tg_send,
                        ))
                    new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    headers = _strip_length_headers(response.headers)
                    response = _StarletteResponse(
                        content=new_body,
                        status_code=200,
                        headers=dict(headers),
                        media_type="application/json",
                    )
            except Exception as e:
                # Body buffering failed; leave response untouched. Settle still succeeded.
                print(f"[RECEIPT] body buffer failed for {endpoint}: {e}")

        # === Post-settle failure detection (5xx + settled) ===================
        elif is_paid_endpoint and settled and response.status_code >= 500:
            # 503 + Retry-After present → settle likely not done → skip alert
            retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
            if not (response.status_code == 503 and retry_after):
                price = float(str(_ENDPOINT_PRICES.get(endpoint, "0.001")))
                # Stats event — always log
                _log_post_settle_failure(
                    endpoint=endpoint, ip=ip, payer=payer or "",
                    tx_hash=transaction or "", amount=price,
                    status_code=response.status_code, error_summary="",
                )
                # Telegram alert — fire-and-forget with dedupe + hourly cap
                asyncio.create_task(_send_post_settle_alert(
                    "post_settle_failure", endpoint, ip,
                    payer or "", transaction or "",
                    price, response.status_code,
                    error_summary=f"HTTP {response.status_code}",
                    tg_send=tg_send,
                ))

        await tg_notify_request(endpoint, symbol, ip, response.status_code,
                                network=network, payer=payer, transaction=transaction)
    except Exception:
        pass
    return _apply_cors_headers(response)

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
        "description": "Korean crypto market + AI analysis + Korean news (K-pop, semiconductor) → English for AI agents. 15 paid endpoints, 189+ tokens. Kimchi Premium, exchange intelligence, AI sentiment, divergence (light/deep), market read, Korean news headlines + AI synthesis.",
        "url": "https://api.printmoneylab.com",
        "mcp": "https://mcp.printmoneylab.com/mcp",
        "source": "https://github.com/bakyang2/kr-crypto-intelligence",
        "llms_txt": "https://api.printmoneylab.com/llms.txt",
        "receipt_signer": {
            "address": _RECEIPT_SIGNER_ADDRESS,
            "public_key": _RECEIPT_SIGNER_PUBLIC_KEY,
            "algorithm": "secp256k1-eth-personal-sign",
            "payload_format": "id|endpoint|amount|currency|network|tx_hash|payer|merchant|issued_at",
        },
        "endpoints": [
            {"path": "/api/v1/kimchi-premium", "method": "GET", "price": "$0.002", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Real-time Kimchi Premium (Upbit vs Binance)"},
            {"path": "/api/v1/kr-prices", "method": "GET", "price": "$0.002", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean exchange prices (Upbit, Bithumb)"},
            {"path": "/api/v1/fx-rate", "method": "GET", "price": "$0.001", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "USD/KRW exchange rate"},
            {"path": "/api/v1/stablecoin-premium", "method": "GET", "price": "$0.002", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "USDT/USDC premium on Korean exchanges (fund flow indicator)"},
            {"path": "/api/v1/arbitrage-scanner", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Token-by-token Kimchi Premium for 189+ tokens, reverse premium, Upbit-Bithumb gaps, market share"},
            {"path": "/api/v1/exchange-alerts", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "New listings/delistings, investment warnings, caution flags"},
            {"path": "/api/v1/market-movers", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "1-min price surges/crashes, volume spikes, top volume tokens"},
            {"path": "/api/v1/market-read", "method": "GET", "price": "$0.10", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "AI market analysis — 12+ data sources + exchange intelligence + Claude AI token-level signals"},
            {"path": "/api/v1/kr-sentiment", "method": "GET", "price": "$0.05", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean crypto sentiment — exchange intelligence + Korean news + AI analysis. First-in-world Korean-to-English crypto sentiment API"},
            {"path": "/api/v1/global-vs-korea-divergence", "method": "GET", "price": "$0.05", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Global vs Korea price divergence (light) — CoinGecko global price + Korean exchange + 1-2 sentence AI summary"},
            {"path": "/api/v1/global-vs-korea-divergence-deep", "method": "GET", "price": "$0.10", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Global vs Korea price divergence (deep) — light response + Korean news signals (Coinness Telegram) + structured AI analysis"},
            {"path": "/api/v1/kr-news/kpop", "method": "GET", "price": "$0.01", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean K-pop news (Naver-aggregated) translated to English with AI relevance classification. Headlines + summary_en + source_en/source_kr."},
            {"path": "/api/v1/kr-news/kpop-summary", "method": "GET", "price": "$0.05", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean K-pop news + AI synthesis (sentiment, key themes, trending entities). Sonnet 4.6 paragraph summary."},
            {"path": "/api/v1/kr-news/semiconductor", "method": "GET", "price": "$0.02", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean semiconductor industry news (Samsung/SK Hynix/HBM/foundry) translated to English with AI relevance classification."},
            {"path": "/api/v1/kr-news/semiconductor-summary", "method": "GET", "price": "$0.10", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "Korean semiconductor news + AI market synthesis with market_signal (bullish/bearish/neutral). Sonnet 4.6."},
            {"path": "/api/v1/krw-macro-stress", "method": "GET", "price": "$0.05", "networks": ["eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], "description": "KRW Macro Stress Score (0-100) — combined signal: US 3Y treasury + VIX + foreign ownership proxy + USD/KRW momentum + Korean semiconductor equity. Rolling 120d percentile."},
            {"path": "/api/v1/xrpl/kimchi-premium", "method": "GET", "price": "$0.002", "networks": ["xrpl:0"], "description": "Real-time Kimchi Premium — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/kr-prices", "method": "GET", "price": "$0.002", "networks": ["xrpl:0"], "description": "Korean exchange prices (Upbit, Bithumb) — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/fx-rate", "method": "GET", "price": "$0.001", "networks": ["xrpl:0"], "description": "USD/KRW exchange rate — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/stablecoin-premium", "method": "GET", "price": "$0.002", "networks": ["xrpl:0"], "description": "USDT/USDC premium on Korean exchanges — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/arbitrage-scanner", "method": "GET", "price": "$0.01", "networks": ["xrpl:0"], "description": "189+ token Kimchi Premium scanner — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/exchange-alerts", "method": "GET", "price": "$0.01", "networks": ["xrpl:0"], "description": "Korean exchange alerts (listings, warnings, cautions) — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/market-movers", "method": "GET", "price": "$0.01", "networks": ["xrpl:0"], "description": "1-min movers + volume spikes — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/market-read", "method": "GET", "price": "$0.10", "networks": ["xrpl:0"], "description": "Full market AI synthesis (12+ sources) — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/kr-sentiment", "method": "GET", "price": "$0.05", "networks": ["xrpl:0"], "description": "Korean crypto sentiment (news + AI) — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/global-vs-korea-divergence", "method": "GET", "price": "$0.05", "networks": ["xrpl:0"], "description": "Global vs Korea divergence (light AI) — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/global-vs-korea-divergence-deep", "method": "GET", "price": "$0.10", "networks": ["xrpl:0"], "description": "Global vs Korea divergence (deep AI + news) — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/kr-news/kpop", "method": "GET", "price": "$0.01", "networks": ["xrpl:0"], "description": "Korean K-pop news translated to English — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/kr-news/kpop-summary", "method": "GET", "price": "$0.05", "networks": ["xrpl:0"], "description": "Korean K-pop news + AI synthesis — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/kr-news/semiconductor", "method": "GET", "price": "$0.02", "networks": ["xrpl:0"], "description": "Korean semiconductor industry news — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/kr-news/semiconductor-summary", "method": "GET", "price": "$0.10", "networks": ["xrpl:0"], "description": "Korean semiconductor news + AI market synthesis — XRPL/RLUSD"},
            {"path": "/api/v1/xrpl/krw-macro-stress", "method": "GET", "price": "$0.05", "networks": ["xrpl:0"], "description": "KRW Macro Stress Score (0-100) — XRPL/RLUSD"}
        ],
        "free_endpoints": [
            {"path": "/api/v1/symbols", "method": "GET", "description": "Available trading symbols"},
            {"path": "/health", "method": "GET", "description": "Service health check"},
            {"path": "/api/v1/stats", "method": "GET", "description": "API usage statistics"}
        ],
        "payment": [
            {"scheme": "exact", "network": "eip155:8453", "asset": "USDC", "payTo": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"},
            {"scheme": "exact", "network": "eip155:137", "asset": "USDC", "payTo": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB"},
            {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "asset": "USDC", "payTo": "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9"},
            {"scheme": "exact", "network": "xrpl:0", "asset": "RLUSD", "payTo": "raKj7ZGoPy1fWw1vfynuJhyHirpcmUMBhP", "issuer": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"}
        ],
        "tags": ["korean", "crypto", "kimchi-premium", "upbit", "bithumb", "fx-rate", "market-data", "asia", "arbitrage", "exchange-intelligence", "ai-analysis", "divergence", "coingecko"]
    }

LLMS_TXT_CONTENT = """# KR Crypto Intelligence API

> Korean crypto market data + AI sentiment analysis for AI agents. Pay per request via x402 on Base, Polygon, Solana, and XRPL. The only Korean crypto market data provider on XRPL — payable in RLUSD.
> API: https://api.printmoneylab.com
> MCP: https://mcp.printmoneylab.com/mcp
> Docs: https://api.printmoneylab.com/docs
> GitHub: https://github.com/bakyang2/kr-crypto-intelligence

## How it works

Every endpoint is callable via the x402 payment protocol.
Your agent sends a standard HTTP request; when payment is required,
it receives HTTP 402 with payment instructions. No API keys, no accounts,
no registration. Payment is settled per-request in USDC (on Base, Polygon,
or Solana) or RLUSD (on XRPL — via /api/v1/xrpl/<endpoint> variants).

## How AI agents pay

This API uses x402 micropayments (HTTP 402 Payment Required protocol).

### Quick start

1. Install an x402-compatible client:
   - AgentCash (recommended for MCP): https://agentcash.dev
   - Pay.sh (Google Cloud / Solana Foundation): https://pay.sh
   - x402 SDK (TypeScript / Python / Go / Java): https://github.com/coinbase/x402
2. Fund your wallet:
   - USDC on Base, Polygon, or Solana mainnet, OR
   - RLUSD on XRPL mainnet (for /api/v1/xrpl/<endpoint> variants)
   - Minimum $0.10 recommended for testing
   - Each API call costs $0.001 to $0.10 (same tiers on all chains)
3. Call any paid endpoint:
   - First call returns HTTP 402 with payment challenge
   - Client signs a stablecoin transfer (USDC or RLUSD; handled automatically)
   - Retry with X-PAYMENT (EVM/Solana) or PAYMENT-SIGNATURE (XRPL) header gets data

All paid endpoints are also reachable via /api/v1/xrpl/<endpoint> for
XRPL/RLUSD settlement — same prices, same responses.

### Merchant wallets

- Base mainnet: 0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB
- Polygon mainnet: 0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB
- Solana mainnet: 3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9
- XRPL mainnet: raKj7ZGoPy1fWw1vfynuJhyHirpcmUMBhP
- RLUSD issuer (XRPL): rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De

### Receipt verification

Every paid response includes a signed receipt (ECDSA secp256k1) for agent accountability.

- Public key: https://api.printmoneylab.com/.well-known/x402 → receipt_signer.public_key
- Verification: Account.recover_message() from the eth_account Python library
- Payload format: id|endpoint|amount|currency|network|tx_hash|payer|merchant|issued_at

### Browser support

CORS preflight allows X-PAYMENT header from any origin. Browser-based agents can
call paid endpoints directly without proxy.

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

### KRW Macro Signal
- GET /api/v1/krw-macro-stress -> $0.05
  KRW Macro Stress Score (0-100). Combined 5-component signal:
  US 3Y treasury (FRED), VIX, foreign ownership proxy on SK Hynix +
  Samsung (mcap-weighted %), USD/KRW momentum, Korean semiconductor
  equity. Rolling 120d percentile over 2yr backfill.
  Returns score + regime (calm/neutral/caution/risk_off/crisis) +
  direction (krw_weakening/stable/strengthening) + per-component
  breakdown + AI-generated factual note (no trading advice).
  15-min cache. Positioning: KRW macro stress signal for trading bots
  filtering entry/exit on macro regime (not a kimchi-premium predictor).

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
- GET /api/v1/kimchi-premium?symbol={SYMBOL} -> $0.002
- GET /api/v1/stablecoin-premium -> $0.002
- GET /api/v1/kr-prices?symbol={SYMBOL}&exchange={EXCHANGE} -> $0.002
- GET /api/v1/fx-rate -> $0.001

### Korean News → English (Naver-aggregated, AI-translated)
- GET /api/v1/kr-news/kpop?limit={1..10} -> $0.01
  Korean K-pop news (artists, groups, soloists, comebacks) translated to
  English with AI relevance classification. Returns title_en + summary_en
  + source_en + original Korean (title_kr/source_kr) for verification.
  5-min cache.
- GET /api/v1/kr-news/kpop-summary?limit={1..10} -> $0.05
  Same articles + Sonnet 4.6 synthesis: overall_sentiment, key_themes,
  trending_entities, paragraph summary.
- GET /api/v1/kr-news/semiconductor?limit={1..10} -> $0.02
  Korean semiconductor industry news (Samsung Electronics, SK Hynix, HBM,
  DRAM/NAND, foundry, suppliers) translated to English. Headlines from
  전자신문/디지털타임스/한국경제/etc.
- GET /api/v1/kr-news/semiconductor-summary?limit={1..10} -> $0.10
  Same articles + Sonnet 4.6 market synthesis: sentiment, key_themes,
  trending_entities, market_signal (bullish/bearish/neutral), paragraph.

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
  Tools: 13 (get_kr_sentiment, get_market_read, get_arbitrage_scanner,
         get_exchange_alerts, get_market_movers, get_kimchi_premium,
         get_stablecoin_premium, get_kr_prices, get_fx_rate,
         get_global_vs_korea_divergence, get_global_vs_korea_divergence_deep,
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

# 1x1 transparent PNG — minimal favicon so AgentCash discovery's FAVICON_MISSING audit passes.
_FAVICON_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f80f00000100010003e8b58e0000000049454e44ae426082"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import Response
    return Response(content=_FAVICON_PNG, media_type="image/png")


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
async def kimchi_premium(request: Request, symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)")):
    track_request("kimchi-premium")
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="kimchi-premium", paid=True, price_usd=0.002, ip=get_real_ip(request))
    symbol = validate_symbol(symbol)
    try:
        upbit = await fetch_upbit_price(symbol)
        if "error" in upbit:
            raise HTTPException(status_code=404, detail=upbit["error"])
        binance = await fetch_binance_price(symbol)
        if "error" in binance:
            raise HTTPException(status_code=404, detail={"message": binance["error"], "suggestion": f"Use /api/v1/kr-prices?symbol={symbol} for Korean-only price data."})
        fx = await fetch_fx_rate()
        upbit_usdt = await fetch_upbit_price("USDT")  # KRW-USDT 시세 (USDT 기반 프리미엄용)
        binance_krw = binance["price_usdt"] * fx["rate"]
        premium_pct = ((upbit["price_krw"] - binance_krw) / binance_krw) * 100

        # USDT 기반 프리미엄: 한국 거래소 가격을 USDT/KRW 실거래가로 USD 환산
        premium_pct_usdt = None
        usdt_krw_rate = None
        if upbit_usdt and "error" not in upbit_usdt:
            usdt_krw_rate = upbit_usdt["price_krw"]
            korea_implied_usd_via_usdt = upbit["price_krw"] / usdt_krw_rate
            premium_pct_usdt = round(
                ((korea_implied_usd_via_usdt - binance["price_usdt"]) / binance["price_usdt"]) * 100,
                2,
            )

        result = {
            "symbol": symbol,
            "upbit_krw": upbit["price_krw"],
            "binance_usdt": binance["price_usdt"],
            "fx_rate": fx["rate"],
            "fx_rate_official": fx["rate"],          # 공식 USD/KRW (별칭)
            "usdt_krw_rate": usdt_krw_rate,          # Upbit KRW-USDT 실거래가
            "fx_source": fx["source"],
            "binance_krw_equivalent": round(binance_krw, 0),
            "premium_percent": round(premium_pct, 2),
            "premium_pct_usdt": premium_pct_usdt,    # USDT 실거래가 기반 프리미엄
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
async def stablecoin_premium(request: Request):
    track_request("stablecoin-premium")
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="stablecoin-premium", paid=True, price_usd=0.002, ip=get_real_ip(request))
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
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol"),
    exchange: str = Query(default="all", description="Exchange: upbit, bithumb, or all")
):
    track_request("kr-prices")
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="kr-prices", paid=True, price_usd=0.002, ip=get_real_ip(request))
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
async def fx_rate_endpoint(request: Request):
    track_request("fx-rate")
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="fx-rate", paid=True, price_usd=0.001, ip=get_real_ip(request))
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
        if not getattr(request.state, "paid_log_via_wrapper", False):
            log_event("api_call", endpoint="kr-sentiment", paid=True, price_usd=0.05, ip=ip)
        # Note: 텔레그램 알림은 미들웨어(line ~655)에서 일괄 발송 — 중복 카운트 방지
        return result
    except Exception as e:
        stats["errors"] += 1
        log_event("error", endpoint="kr-sentiment", error=str(e)[:200])
        raise HTTPException(status_code=503, detail=f"Sentiment analysis failed: {str(e)[:100]}")


# === krw-macro-stress endpoint ===
@app.get("/api/v1/krw-macro-stress")
async def krw_macro_stress_endpoint(request: Request):
    """KRW Macro Stress Score — combined 5-component signal (0-100)."""
    track_request("/api/v1/krw-macro-stress")
    ip = get_real_ip(request)
    try:
        result = await fetch_krw_macro_stress()
        if not getattr(request.state, "paid_log_via_wrapper", False):
            log_event("api_call", endpoint="krw-macro-stress", paid=True, price_usd=0.05, ip=ip)
        return result
    except Exception as e:
        stats["errors"] += 1
        log_event("error", endpoint="krw-macro-stress", error=str(e)[:200])
        raise HTTPException(status_code=503, detail=f"KRW macro stress failed: {str(e)[:100]}")


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
        upbit_usdt = await fetch_upbit_price("USDT")  # KRW-USDT 시세
        if not all([upbit, binance, fx, upbit_usdt]):
            return None
        upbit_krw = upbit["price_krw"]
        binance_krw_official = binance["price_usdt"] * fx["rate"]
        premium_official = ((upbit_krw - binance_krw_official) / binance_krw_official) * 100

        # USDT 기반: 한국 거래소 BTC 가격을 USDT/KRW 실거래가로 USD 환산
        usdt_krw_rate = upbit_usdt["price_krw"]
        korea_implied_usd_via_usdt = upbit_krw / usdt_krw_rate
        premium_usdt = ((korea_implied_usd_via_usdt - binance["price_usdt"]) / binance["price_usdt"]) * 100

        return {
            "symbol": symbol,
            "premium_pct": round(premium_official, 2),       # 기존 (공식 USD/KRW)
            "premium_pct_usdt": round(premium_usdt, 2),      # 신규 (USDT 실거래가 기반)
            "upbit_krw": upbit_krw,
            "binance_usd": binance["price_usdt"],
            "fx_rate_official": fx["rate"],                  # 공식 USD/KRW
            "usdt_krw_rate": usdt_krw_rate,                  # USDT/KRW 실거래가
            "direction": "positive" if premium_official > 0 else "negative",
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
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="arbitrage-scanner", paid=True, price_usd=0.01, ip=get_real_ip(request))
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
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="exchange-alerts", paid=True, price_usd=0.01, ip=get_real_ip(request))
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
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="market-movers", paid=True, price_usd=0.01, ip=get_real_ip(request))
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
    if not getattr(request.state, "paid_log_via_wrapper", False):
        log_event("api_call", endpoint="market-read", paid=True, price_usd=0.10, ip=ip)

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


async def _serve_divergence(symbol: str, depth_norm: str, endpoint_label: str, price_usd: float, ttl: int, ip: str = "", request: Request | None = None):
    """Shared light/deep dispatcher with per-key cache + lock.

    `request` (optional) — when the caller is an XRPL wrapper it will have
    set `request.state.paid_log_via_wrapper = True`, in which case the
    dispatcher's own log_event is suppressed and the wrapper's XRPL-labelled
    log_event is the sole api_call entry."""
    _skip_log = bool(request is not None and getattr(request.state, "paid_log_via_wrapper", False))
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
        if not _skip_log:
            log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd, ip=ip)
        return data

    # Slow path with per-key lock
    lock = _get_divergence_lock(cache_key)
    async with lock:
        now = time.time()
        cached_entry = _divergence_cache.get(cache_key)
        if cached_entry and now < cached_entry[1]:
            data = dict(cached_entry[0])
            data["data_age_seconds"] = int(now - (cached_entry[1] - ttl))
            if not _skip_log:
                log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd, ip=ip)
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
        if not _skip_log:
            log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd, ip=ip)
        return result


@app.get("/api/v1/global-vs-korea-divergence")
async def global_vs_korea_divergence(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    """Light tier — divergence + 1-2 sentence AI summary. $0.05."""
    track_request("/api/v1/global-vs-korea-divergence")
    return await _serve_divergence(symbol, "light", "global-vs-korea-divergence", 0.05, DIVERGENCE_LIGHT_TTL, get_real_ip(request), request=request)


@app.get("/api/v1/global-vs-korea-divergence-deep")
async def global_vs_korea_divergence_deep(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    """Deep tier — light data + Korean news signal + structured AI analysis. $0.10."""
    track_request("/api/v1/global-vs-korea-divergence-deep")
    return await _serve_divergence(symbol, "deep", "global-vs-korea-divergence-deep", 0.10, DIVERGENCE_DEEP_TTL, get_real_ip(request), request=request)


# ============================================================
# Korean News → English (4 endpoints)
# ============================================================
KR_NEWS_TIMEOUT = 45  # 5-min cache hit < 1s; cold call 13~40s (Sonnet variable); cap below typical client SLA


async def _serve_kr_news(category: str, premium: bool, limit: int, ip: str, endpoint_label: str, price_usd: float, request: Request | None = None):
    """Shared dispatcher for the 4 kr-news endpoints. Wraps fetch_kr_news with a
    timeout so a cold call that exceeds the budget returns 503 instead of hanging.

    `request` (optional) — XRPL wrapper sets request.state.paid_log_via_wrapper
    to True; when so, the dispatcher skips its own log_event so the wrapper's
    xrpl-labelled api_call is the single stats.jsonl entry."""
    _skip_log = bool(request is not None and getattr(request.state, "paid_log_via_wrapper", False))
    try:
        result = await asyncio.wait_for(
            fetch_kr_news(category=category, premium=premium, limit=limit),
            timeout=KR_NEWS_TIMEOUT,
        )
        if not _skip_log:
            log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd, ip=ip)
        if not result.get("ok") and result.get("error"):
            # Naver/Claude returned no usable data — surface as 503 with retry hint
            return JSONResponse(
                status_code=503,
                content={"error": result.get("error", "no news"), "category": category, "retry_after_seconds": 60},
                headers={"Retry-After": "60"},
            )
        return result
    except asyncio.TimeoutError:
        stats["errors"] += 1
        log_event("error", endpoint=endpoint_label, error="timeout", ip=ip)
        return JSONResponse(
            status_code=503,
            content={
                "error": "Service is generating fresh news, retry in 60 seconds",
                "category": category,
                "retry_after_seconds": 60,
            },
            headers={"Retry-After": "60"},
        )
    except Exception as e:
        stats["errors"] += 1
        print(f"[KR-NEWS] handler error ({endpoint_label}): {e}")
        log_event("error", endpoint=endpoint_label, error=str(e)[:200], ip=ip)
        raise HTTPException(status_code=503, detail=f"kr-news failed: {str(e)[:120]}")


@app.get("/api/v1/kr-news/kpop")
async def kr_news_kpop(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to return (1-10)"),
):
    """Korean K-pop news (Naver) → English translation + AI relevance classification. $0.01."""
    track_request("/api/v1/kr-news/kpop")
    return await _serve_kr_news("kpop", False, limit, get_real_ip(request), "kr-news/kpop", 0.01, request=request)


@app.get("/api/v1/kr-news/kpop-summary")
async def kr_news_kpop_summary(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to analyze (1-10)"),
):
    """Korean K-pop news + AI synthesis (sentiment, key themes, trending artists). $0.05."""
    track_request("/api/v1/kr-news/kpop-summary")
    return await _serve_kr_news("kpop", True, limit, get_real_ip(request), "kr-news/kpop-summary", 0.05, request=request)


@app.get("/api/v1/kr-news/semiconductor")
async def kr_news_semiconductor(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to return (1-10)"),
):
    """Korean semiconductor industry news (Naver) → English translation. Samsung/SK Hynix/HBM. $0.02."""
    track_request("/api/v1/kr-news/semiconductor")
    return await _serve_kr_news("semiconductor", False, limit, get_real_ip(request), "kr-news/semiconductor", 0.02, request=request)


@app.get("/api/v1/kr-news/semiconductor-summary")
async def kr_news_semiconductor_summary(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to analyze (1-10)"),
):
    """Korean semiconductor news + AI market synthesis (sentiment, themes, market_signal). $0.10."""
    track_request("/api/v1/kr-news/semiconductor-summary")
    return await _serve_kr_news("semiconductor", True, limit, get_real_ip(request), "kr-news/semiconductor-summary", 0.10, request=request)


# =============================================================================
# XRPL variants (Path C — 15 routes)
# =============================================================================
# Settle is handled by x402-xrpl require_payment middleware (registered above
# in 6 price buckets). By the time these wrappers run, RLUSD has already been
# received by the merchant XRPL wallet.
#
# Each wrapper:
#   1. Sets request.state.paid_log_via_wrapper = True — signals the original
#      handler / shared dispatcher (via Option C guard) to skip its own
#      log_event so we don't double-count.
#   2. Emits its own log_event with endpoint="xrpl/<name>" — chain-separated
#      revenue accounting in the daily report (stats.jsonl `api_call`).
#   3. Delegates to the existing production handler / shared dispatcher, which
#      does the actual work (identical business logic, identical response).
#
# XRPL settle is picked up by rate_limit_middleware's PAYMENT-RESPONSE decoder
# → tg_notify_request (payment_settled event, endpoint "xrpl/<name>") → the
# _create_receipt currency dispatch chooses RLUSD (main.py:1653+).

def _mark_xrpl(request: Request, endpoint_label: str, price_usd: float, ip: str) -> None:
    """Signal + log for XRPL wrappers. One-liner shared by all 15 wrappers."""
    request.state.paid_log_via_wrapper = True
    track_request(endpoint_label)
    log_event("api_call", endpoint=endpoint_label, paid=True, price_usd=price_usd, ip=ip)


@app.get("/api/v1/xrpl/kimchi-premium")
async def kimchi_premium_xrpl(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    _mark_xrpl(request, "xrpl/kimchi-premium", 0.002, get_real_ip(request))
    return await kimchi_premium(request, symbol)


@app.get("/api/v1/xrpl/kr-prices")
async def kr_prices_xrpl(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol"),
    exchange: str = Query(default="all", description="Exchange: upbit, bithumb, or all"),
):
    _mark_xrpl(request, "xrpl/kr-prices", 0.002, get_real_ip(request))
    return await kr_prices(request, symbol, exchange)


@app.get("/api/v1/xrpl/fx-rate")
async def fx_rate_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/fx-rate", 0.001, get_real_ip(request))
    return await fx_rate_endpoint(request)


@app.get("/api/v1/xrpl/stablecoin-premium")
async def stablecoin_premium_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/stablecoin-premium", 0.002, get_real_ip(request))
    return await stablecoin_premium(request)


@app.get("/api/v1/xrpl/arbitrage-scanner")
async def arbitrage_scanner_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/arbitrage-scanner", 0.01, get_real_ip(request))
    return await arbitrage_scanner(request)


@app.get("/api/v1/xrpl/exchange-alerts")
async def exchange_alerts_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/exchange-alerts", 0.01, get_real_ip(request))
    return await exchange_alerts(request)


@app.get("/api/v1/xrpl/market-movers")
async def market_movers_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/market-movers", 0.01, get_real_ip(request))
    return await market_movers(request)


@app.get("/api/v1/xrpl/market-read")
async def market_read_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/market-read", 0.10, get_real_ip(request))
    return await market_read(request)


@app.get("/api/v1/xrpl/kr-sentiment")
async def kr_sentiment_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/kr-sentiment", 0.05, get_real_ip(request))
    return await kr_sentiment_endpoint(request)


@app.get("/api/v1/xrpl/krw-macro-stress")
async def krw_macro_stress_xrpl(request: Request):
    _mark_xrpl(request, "xrpl/krw-macro-stress", 0.05, get_real_ip(request))
    return await krw_macro_stress_endpoint(request)


@app.get("/api/v1/xrpl/global-vs-korea-divergence")
async def global_vs_korea_divergence_xrpl(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    _mark_xrpl(request, "xrpl/global-vs-korea-divergence", 0.05, get_real_ip(request))
    return await global_vs_korea_divergence(request, symbol)


@app.get("/api/v1/xrpl/global-vs-korea-divergence-deep")
async def global_vs_korea_divergence_deep_xrpl(
    request: Request,
    symbol: str = Query(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP)"),
):
    _mark_xrpl(request, "xrpl/global-vs-korea-divergence-deep", 0.10, get_real_ip(request))
    return await global_vs_korea_divergence_deep(request, symbol)


@app.get("/api/v1/xrpl/kr-news/kpop")
async def kr_news_kpop_xrpl(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to return (1-10)"),
):
    _mark_xrpl(request, "xrpl/kr-news/kpop", 0.01, get_real_ip(request))
    return await kr_news_kpop(request, limit)


@app.get("/api/v1/xrpl/kr-news/kpop-summary")
async def kr_news_kpop_summary_xrpl(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to analyze (1-10)"),
):
    _mark_xrpl(request, "xrpl/kr-news/kpop-summary", 0.05, get_real_ip(request))
    return await kr_news_kpop_summary(request, limit)


@app.get("/api/v1/xrpl/kr-news/semiconductor")
async def kr_news_semiconductor_xrpl(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to return (1-10)"),
):
    _mark_xrpl(request, "xrpl/kr-news/semiconductor", 0.02, get_real_ip(request))
    return await kr_news_semiconductor(request, limit)


@app.get("/api/v1/xrpl/kr-news/semiconductor-summary")
async def kr_news_semiconductor_summary_xrpl(
    request: Request,
    limit: int = Query(default=5, ge=1, le=10, description="Number of articles to analyze (1-10)"),
):
    _mark_xrpl(request, "xrpl/kr-news/semiconductor-summary", 0.10, get_real_ip(request))
    return await kr_news_semiconductor_summary(request, limit)
