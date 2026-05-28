import httpx
import os
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from pydantic import Field
from stats_logger import log_event
from client_classifier import classify, extract_real_ip

TG_BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
STATS_PATH = os.getenv(
    "STATS_JSONL_FILE",
    "/home/ubuntu/KRCryptoAPI/stats.jsonl",
)
KST = timezone(timedelta(hours=9))

# === Telegram ===============================================================
async def _tg_notify(text):
    if not TG_BOT or not TG_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
                json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
            )
    except Exception:
        pass


# === Recent payments cache (IP → 24h paid count) ===========================
# Used by classifier to elevate paying IPs to Tier 1 even when UA is unknown.
_RECENT_PAYMENTS_CACHE = {}
_RECENT_PAYMENTS_TS = 0
_RECENT_PAYMENTS_TTL = 300  # 5-min refresh


def _build_recent_payments_cache() -> dict:
    cache = {}
    cutoff = time.time() - 86400
    try:
        with open(STATS_PATH) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") == "payment_settled" and e.get("ts", 0) >= cutoff:
                    ip = e.get("ip", "")
                    if ip:
                        cache[ip] = cache.get(ip, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as ex:
        print(f"[MCP-CLASSIFY] payment cache build error: {ex}")
    return cache


def _get_recent_payments(ip: str) -> int:
    global _RECENT_PAYMENTS_CACHE, _RECENT_PAYMENTS_TS
    now = time.time()
    if now - _RECENT_PAYMENTS_TS > _RECENT_PAYMENTS_TTL:
        _RECENT_PAYMENTS_CACHE = _build_recent_payments_cache()
        _RECENT_PAYMENTS_TS = now
    return _RECENT_PAYMENTS_CACHE.get(ip, 0)


# === Tier 6 burst tracker + first-seen UA registry =========================
# Per-IP rolling 1h counter for suspicious-rate detection.
_HOURLY_IP_COUNT = {}        # ip -> [timestamps in last hour]
_FIRST_SEEN_UA = set()       # User-Agent strings already alerted on
_SEEN_LOAD_TS = 0


def _load_seen_uas_from_stats():
    """One-time cold load of UAs already observed in stats.jsonl so the
    first-seen alert doesn't fire for every UA on the first restart."""
    global _FIRST_SEEN_UA, _SEEN_LOAD_TS
    if _SEEN_LOAD_TS:
        return
    try:
        with open(STATS_PATH) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") == "mcp_call":
                    ua = e.get("user_agent")
                    if ua:
                        _FIRST_SEEN_UA.add(ua)
    except FileNotFoundError:
        pass
    except Exception as ex:
        print(f"[MCP-CLASSIFY] seen-UA load error: {ex}")
    _SEEN_LOAD_TS = time.time()


def _is_first_seen_ua(ua: str) -> bool:
    if not ua:
        return False
    _load_seen_uas_from_stats()
    if ua in _FIRST_SEEN_UA:
        return False
    _FIRST_SEEN_UA.add(ua)
    return True


def _record_ip_hit(ip: str) -> int:
    """Append now to ip's rolling 1h hit list, prune old, return current count."""
    if not ip or ip == "-":
        return 0
    now = time.time()
    cutoff = now - 3600
    arr = _HOURLY_IP_COUNT.get(ip, [])
    arr = [t for t in arr if t >= cutoff]
    arr.append(now)
    _HOURLY_IP_COUNT[ip] = arr
    return len(arr)


def _ip_emoji(country: str) -> str:
    """Pure-string flag-ish prefix; we don't call ipinfo from the MCP path
    (would add latency). Country comes from Cloudflare's cf-ipcountry header."""
    if not country:
        return ""
    return f" {country}"


# === Alert formatters =======================================================
def _now_kst_hms() -> str:
    return datetime.now(KST).strftime("%H:%M:%S KST")


def _format_tier_alert(tier, client_name, tool_name, ip, details) -> str:
    emoji = {1: "💎", 2: "🔵", 3: "🟡"}[tier]
    label = {1: "PAID USER", 2: "AI CLIENT", 3: "AGENT FRAMEWORK"}[tier]
    country = _ip_emoji(details.get("country", ""))
    lines = [
        f"{emoji} <b>MCP 호출 [{label}]</b>",
        f"Tool: {tool_name}",
        f"Client: {client_name}",
        f"IP: {ip}{country}",
    ]
    if tier == 1:
        lines.append(f"24h 결제: {details.get('recent_payment_count_24h', 0)}건")
    if tier in (2, 3):
        lines.append(
            "결제 헤더: " + ("있음" if details.get("has_payment_header") else "없음 (사용 시도 추정)")
        )
        ua = details.get("user_agent", "")
        if ua:
            lines.append(f"User-Agent: {ua[:80]}")
    lines.append(f"시간: {_now_kst_hms()}")
    return "\n".join(lines)


def _format_unknown_alert(tool_name, ip, details) -> str:
    return (
        f"❓ <b>새로운 MCP 클라이언트 발견</b>\n"
        f"Tool: {tool_name}\n"
        f"IP: {ip}{_ip_emoji(details.get('country', ''))}\n"
        f"User-Agent: {(details.get('user_agent') or '')[:120]}\n"
        f"→ 분류 추가 검토 필요\n"
        f"시간: {_now_kst_hms()}"
    )


def _format_suspicious_alert(ip, hourly_count, details) -> str:
    return (
        f"🔴 <b>의심 활동 감지</b>\n"
        f"IP: {ip}{_ip_emoji(details.get('country', ''))}\n"
        f"패턴: User-Agent 없음 + 시간당 {hourly_count}회 호출\n"
        f"User-Agent raw: {(details.get('user_agent') or '<empty>')[:80]}\n"
        f"권장: 차단 검토\n"
        f"시간: {_now_kst_hms()}"
    )


# Suspicious-alert dedupe (per IP, 1h)
_SUSPICIOUS_ALERTED = {}        # ip -> unix ts of last alert
_SUSPICIOUS_HOURLY_THRESHOLD = 50


async def _send_mcp_alert(tier, client_name, tool_name, ip, details):
    """Tier-based dispatcher.

    - Tier 1/2/3: realtime alert
    - Tier 4/5: silent (rolled into daily summary)
    - Tier 6 / Unknown: rate-gated alert
    """
    try:
        # Unknown client (well-formed UA but unmatched) — first-seen only
        if client_name.startswith("Unknown:"):
            if _is_first_seen_ua(details.get("user_agent", "")):
                await _tg_notify(_format_unknown_alert(tool_name, ip, details))
            return

        if tier in (4, 5):
            return  # silent; daily summary will surface counts

        if tier == 6:
            hourly = _record_ip_hit(ip)
            if hourly >= _SUSPICIOUS_HOURLY_THRESHOLD:
                now = time.time()
                last = _SUSPICIOUS_ALERTED.get(ip, 0)
                if now - last >= 3600:  # one alert/hour per IP max
                    _SUSPICIOUS_ALERTED[ip] = now
                    await _tg_notify(_format_suspicious_alert(ip, hourly, details))
            return

        # Tier 1, 2, 3 — realtime
        if tier in (1, 2, 3):
            await _tg_notify(_format_tier_alert(tier, client_name, tool_name, ip, details))
    except Exception as e:
        print(f"[MCP-CLASSIFY] alert dispatch failed: {e}")


def _track(tool_name):
    """Log enriched mcp_call event + tier-based telegram alert.
    Pulls request context via FastMCP's get_http_request() helper."""
    try:
        request = get_http_request()
    except Exception:
        request = None

    if request is not None:
        real_ip = extract_real_ip(request)
        recent_payments = _get_recent_payments(real_ip)
        tier, client_name, client_type, details = classify(request, recent_payments)
        client_host = (request.client.host if request.client else "-") if hasattr(request, "client") else "-"
    else:
        tier, client_name, client_type = (None, "Unknown (no request)", "unknown")
        details = {"user_agent": "", "real_ip": "-", "has_payment_header": False,
                   "is_smithery_query": False, "recent_payment_count_24h": 0,
                   "origin": "", "referer": "", "country": ""}
        real_ip = "-"
        client_host = "-"

    log_event(
        "mcp_call",
        tool=tool_name,
        ip=client_host,
        real_ip=real_ip,
        user_agent=details.get("user_agent", ""),
        client_name=client_name,
        client_type=client_type,
        tier=tier,
        has_payment=details.get("has_payment_header", False),
        is_smithery_query=details.get("is_smithery_query", False),
        recent_payments_24h=details.get("recent_payment_count_24h", 0),
        origin=details.get("origin", ""),
        referer=details.get("referer", ""),
        country=details.get("country", ""),
    )
    if tier is not None:
        asyncio.create_task(_send_mcp_alert(tier, client_name, tool_name, real_ip, details))

API_BASE = "http://127.0.0.1:80"


async def _call_paid_api(endpoint: str, params: dict = None) -> dict:
    """Call a paid API endpoint with proper 402 handling.
    On 402: return clear payment instructions (status='payment_required') so
    MCP clients show actionable guidance instead of an empty {} body.
    On 200: return parsed JSON. On other errors: return {error, status_code}."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API_BASE}{endpoint}", params=params or {})

        if r.status_code == 402:
            # Extract price from x402 'payment-required' header (base64 JSON).
            # Fallback: x402 payload may also be the response body itself.
            price_str = "$0.001~$0.10"
            price_float = None
            try:
                import base64, json as _json
                pr_header = r.headers.get("payment-required") or r.headers.get("x-payment-required")
                payload = None
                if pr_header:
                    try:
                        payload = _json.loads(base64.b64decode(pr_header))
                    except Exception:
                        payload = None
                if payload is None:
                    try:
                        payload = r.json()
                    except Exception:
                        payload = None
                if payload and isinstance(payload, dict):
                    accepts = payload.get("accepts", [])
                    if accepts:
                        amt = accepts[0].get("amount") or accepts[0].get("maxAmountRequired")
                        if amt is not None:
                            try:
                                # USDC has 6 decimals
                                price_float = int(amt) / 1_000_000
                                price_str = f"${price_float:.4f}"
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass

            full_endpoint_url = f"https://api.printmoneylab.com{endpoint}"
            return {
                "status": "payment_required",
                "endpoint": endpoint,
                "endpoint_url": full_endpoint_url,
                "price": f"{price_str} USDC",
                "price_usd": price_float,
                "networks": ["Base", "Polygon", "Solana"],
                "merchant_wallets": {
                    "base": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB",
                    "polygon": "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB",
                    "solana": "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9",
                },
                "message": (
                    f"This endpoint requires {price_str} USDC payment via x402 protocol. "
                    f"MCP tools currently return free metadata only. "
                    f"For actual data, call the HTTP API directly with an x402-capable client."
                ),
                "quick_start": [
                    "1. Install an x402 client: AgentCash (https://agentcash.dev) or Pay.sh (https://pay.sh)",
                    "2. Fund your wallet with $0.10+ USDC on Base, Polygon, or Solana",
                    "3. The client auto-handles x402 payment on retry — no manual signing",
                ],
                "compatible_clients": [
                    {"name": "AgentCash", "url": "https://agentcash.dev", "type": "MCP-native CLI wallet"},
                    {"name": "Pay.sh", "url": "https://pay.sh", "type": "Google Cloud + Solana Foundation"},
                    {"name": "x402 SDK", "url": "https://github.com/coinbase/x402", "type": "TypeScript/Python/Go/Java"},
                ],
                "receipt_info": (
                    "Every paid response includes a signed receipt (ECDSA secp256k1) "
                    "for agent accountability. Verifier public key at /.well-known/x402."
                ),
                "documentation": {
                    "manifest": "https://api.printmoneylab.com/.well-known/x402",
                    "llms_txt": "https://api.printmoneylab.com/llms.txt",
                    "openapi": "https://api.printmoneylab.com/openapi.json",
                    "x402_spec": "https://x402.org",
                },
            }

        try:
            return r.json()
        except Exception:
            return {"error": "invalid response", "status_code": r.status_code}


mcp = FastMCP("KR Crypto Intelligence")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kimchi_premium(symbol: str = Field(default="BTC", description="Crypto symbol to check premium for (e.g., BTC, ETH, XRP)")) -> dict:
    """Get real-time Kimchi Premium — the price difference between Korean exchanges (Upbit) and global exchanges (Binance). South Korea ranks top 3 globally in crypto trading volume. A positive premium means Korean traders are paying more than the global market price.

    💰 Price: $0.001 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Returns: premium_percent (official USD/KRW basis), premium_pct_usdt (Upbit USDT live rate basis), upbit_krw, binance_usdt, fx_rate. Gap between the two premium values reveals real arbitrage margin after stablecoin conversion costs.

    Args:
        symbol: Crypto symbol (e.g., BTC, ETH, XRP, SOL, DOGE)
    """
    _track("get_kimchi_premium")
    return await _call_paid_api("/api/v1/kimchi-premium", {"symbol": symbol})

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kr_prices(symbol: str = Field(default="BTC", description="Crypto symbol to query (e.g., BTC, ETH, XRP)"), exchange: str = Field(default="all", description="Exchange to query: upbit, bithumb, or all")) -> dict:
    """Get cryptocurrency prices from Korean exchanges (Upbit, Bithumb). Returns KRW-denominated prices, 24h volume, and change rate.

    💰 Price: $0.001 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Args:
        symbol: Crypto symbol (e.g., BTC, ETH, XRP, SOL, DOGE)
        exchange: Exchange to query — 'upbit', 'bithumb', or 'all' for both
    """
    _track("get_kr_prices")
    return await _call_paid_api("/api/v1/kr-prices", {"symbol": symbol, "exchange": exchange})

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_fx_rate() -> dict:
    """Get current USD/KRW exchange rate. Essential for converting between Korean Won and US Dollar prices.

    💰 Price: $0.001 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_fx_rate")
    return await _call_paid_api("/api/v1/fx-rate")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_available_symbols() -> dict:
    """Get all available trading symbols on Korean exchanges. Returns symbols available on Upbit, Bithumb, and those common to both. Use this to check which symbols you can query before calling other tools.

    💰 Price: FREE (no x402 payment required)
    """
    _track("get_available_symbols")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/api/v1/symbols")
        return r.json()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_stablecoin_premium() -> dict:
    """Get USDT and USDC premium on Korean exchanges vs official USD/KRW rate. Positive premium = capital flowing INTO Korean crypto market. Negative premium = capital flowing OUT. Key indicator of Korean market fund flow direction, separate from Kimchi Premium.

    💰 Price: $0.001 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_stablecoin_premium")
    return await _call_paid_api("/api/v1/stablecoin-premium")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def check_health() -> dict:
    """Check service health and exchange connectivity status. Returns status of Upbit, Bithumb, and Binance API connections.

    💰 Price: FREE (no x402 payment required)
    """
    _track("check_health")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_BASE}/health")
        return r.json()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_arbitrage_scanner() -> dict:
    """Scan Kimchi Premium for ALL tokens (180+) traded on both Upbit and Binance. Returns token-by-token premium %, reverse premiums (negative = Korean discount), Upbit vs Bithumb price gaps, market share between exchanges. Each token includes warning flags, volume soaring alerts, deposit soaring alerts. Updated every 60 seconds. Essential for cross-exchange arbitrage analysis.

    💰 Price: $0.01 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_arbitrage_scanner")
    return await _call_paid_api("/api/v1/arbitrage-scanner")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_exchange_alerts() -> dict:
    """Get Korean exchange alerts: new listings, delistings, investment warnings, and caution flags. Detects INVESTMENT_WARNING, PRICE_FLUCTUATIONS, VOLUME_SOARING, DEPOSIT_SOARING, GLOBAL_PRICE_DIFF, SMALL_ACCOUNTS_CONCENTRATION. New listings/delistings detected by comparing market list changes every 60 seconds. Critical for risk management and early listing detection.

    💰 Price: $0.01 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_exchange_alerts")
    return await _call_paid_api("/api/v1/exchange-alerts")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_market_movers() -> dict:
    """Get Korean market movers: 1-minute price surges/crashes (>1%), volume spikes, and top 20 tokens by trading volume on Upbit. Detects rapid price movements and unusual volume activity in Korean crypto markets. Korean retail activity often leads global price movements — early signal for traders.

    💰 Price: $0.01 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_market_movers")
    return await _call_paid_api("/api/v1/market-movers")

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_market_read() -> dict:
    """AI-powered Korean crypto market analysis. Combines Kimchi Premium, stablecoin premium, FX rate, Upbit/Bithumb volume rankings, Binance funding rate, open interest, BTC dominance, and Fear & Greed index. Returns AI-generated signal (BULLISH/BEARISH/NEUTRAL), confidence score, actionable summary, and all raw data.

    💰 Price: $0.10 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_market_read")
    return await _call_paid_api("/api/v1/market-read")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kr_sentiment() -> dict:
    """Korean crypto market sentiment analysis in English. Combines exchange intelligence (189+ tokens premium, warnings, volume spikes) with Korean news context (Coinness Telegram) for AI-powered real-time insights. First-in-world Korean-to-English crypto sentiment API. Returns sentiment label, score (-1 to +1), English report, exchange signals, news context. 1-hour cache.

    💰 Price: $0.05 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402
    """
    _track("get_kr_sentiment")
    return await _call_paid_api("/api/v1/kr-sentiment")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_global_vs_korea_divergence(symbol: str = Field(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB)")) -> dict:
    """Light tier — premium between CoinGecko global price and Upbit Korean price + 1-2 sentence AI interpretation. 25 supported symbols. 60s cache. Returns prices (global_usd, korea_krw, fx_rate), divergence (premium_pct, direction, magnitude), context_signals (investment_warning, volume_spike_24h), and ai_interpretation (1-2 sentence English summary).

    💰 Price: $0.05 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Args:
        symbol: Crypto symbol — supported: BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB
    """
    _track("get_global_vs_korea_divergence")
    return await _call_paid_api("/api/v1/global-vs-korea-divergence", {"symbol": symbol})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_global_vs_korea_divergence_deep(symbol: str = Field(default="BTC", description="Crypto symbol (e.g., BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB)")) -> dict:
    """Deep tier — light data + Korean news signals (Coinness Telegram, 24h window) + structured AI breakdown (drivers, global context, action suggestion, confidence). 5-min cache. Returns light response fields plus recent_news_signal (korean_news_count_24h, sentiment_score, top_keywords) and ai_deep_analysis (summary, korean_market_drivers, global_context, implied_action_suggestion, confidence).

    💰 Price: $0.10 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Args:
        symbol: Crypto symbol — supported: BTC, ETH, XRP, SOL, ADA, DOGE, DOT, MATIC, LINK, AVAX, ATOM, UNI, LTC, NEAR, OP, ARB, APT, ALGO, FTM, SUI, TRX, BCH, ETC, HBAR, SHIB
    """
    _track("get_global_vs_korea_divergence_deep")
    return await _call_paid_api("/api/v1/global-vs-korea-divergence-deep", {"symbol": symbol})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kr_news_kpop(limit: int = Field(default=5, ge=1, le=10, description="Number of articles to return (1-10)")) -> dict:
    """Korean K-pop news (artists, groups, soloists, comebacks, music releases) aggregated from Naver and translated to English with AI relevance classification. Korean entertainment news often moves global fan markets before English coverage. 5-min cache.

    💰 Price: $0.01 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Returns: results[] with title_en + summary_en + source_en plus original Korean (title_kr/source_kr) for verification, published_at, link.

    Args:
        limit: Number of articles to return (1-10, default 5)
    """
    _track("get_kr_news_kpop")
    return await _call_paid_api("/api/v1/kr-news/kpop", {"limit": limit})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kr_news_kpop_summary(limit: int = Field(default=5, ge=1, le=10, description="Number of articles to analyze (1-10)")) -> dict:
    """Korean K-pop news plus AI synthesis. Same Naver-aggregated, English-translated articles as get_kr_news_kpop, with an added AI analysis layer: overall_sentiment, key_themes, trending_entities (artists/groups), and a paragraph summary. 5-min cache.

    💰 Price: $0.05 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Returns: results[] (translated articles) plus ai_analysis with overall_sentiment, key_themes, trending_entities, summary_en.

    Args:
        limit: Number of articles to analyze (1-10, default 5)
    """
    _track("get_kr_news_kpop_summary")
    return await _call_paid_api("/api/v1/kr-news/kpop-summary", {"limit": limit})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kr_news_semiconductor(limit: int = Field(default=5, ge=1, le=10, description="Number of articles to return (1-10)")) -> dict:
    """Korean semiconductor industry news (Samsung Electronics, SK Hynix, HBM, DRAM/NAND, foundry, AI chips, equipment suppliers) aggregated from Naver and translated to English. Korean chip news leads global semiconductor supply-chain signals. 5-min cache.

    💰 Price: $0.02 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Returns: results[] with title_en + summary_en + source_en plus original Korean (title_kr/source_kr) for verification, published_at, link.

    Args:
        limit: Number of articles to return (1-10, default 5)
    """
    _track("get_kr_news_semiconductor")
    return await _call_paid_api("/api/v1/kr-news/semiconductor", {"limit": limit})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def get_kr_news_semiconductor_summary(limit: int = Field(default=5, ge=1, le=10, description="Number of articles to analyze (1-10)")) -> dict:
    """Korean semiconductor news plus AI market synthesis. Same Naver-aggregated, English-translated articles as get_kr_news_semiconductor, with an added AI layer: overall_sentiment, key_themes, trending_entities, market_signal (bullish/bearish/neutral), and a paragraph summary. 5-min cache.

    💰 Price: $0.10 USDC per call
    💳 Payment: x402 micropayment on Base, Polygon, or Solana
    🔧 Client: AgentCash, Pay.sh, or any x402 SDK
    📖 Docs: https://api.printmoneylab.com/.well-known/x402

    Returns: results[] (translated articles) plus ai_analysis with overall_sentiment, key_themes, trending_entities, market_signal, summary_en.

    Args:
        limit: Number of articles to analyze (1-10, default 5)
    """
    _track("get_kr_news_semiconductor_summary")
    return await _call_paid_api("/api/v1/kr-news/semiconductor-summary", {"limit": limit})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8443)

import anthropic
import asyncio
import os

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
