"""
kr_sentiment.py — Korean crypto market sentiment analysis
Coinness Telegram crawler + Claude Haiku sentiment analysis
Lazy invocation: only called when API request arrives, never background
"""

import os
import re
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from stats_logger import log_event
from patch_exchange_intel import compute_intel_data

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kr_sentiment_cache.json")

# === Korean keyword filter ===

KR_KEYWORDS = [
    # 거래소
    "업비트", "빗썸", "코인원", "고팍스", "코빗", "프라이빗", "지닥", "코빗",
    "Upbit", "Bithumb", "Coinone", "Korbit", "Gopax",
    # 규제/정부
    "금융위", "금감원", "금융정보분석원", "FIU", "금융위원회", "금융감독원",
    "기획재정부", "국세청", "가상자산", "가상자산이용자보호법", "특금법",
    "DAXA", "닥사",
    # 통화/시장
    "원화", "KRW", "한화", "김치프리미엄", "김프", "역프",
    # 지역
    "한국", "대한민국", "국내", "서울", "Korea", "Korean", "South Korea", "KR",
    # 시장/이슈
    "상장", "상폐", "거래지원 종료", "신규종목", "투자경고", "투자주의",
    "입금중단", "출금중단",
    # 정치/기업
    "이재명", "윤석열", "민주당", "국민의힘",
    "네이버", "카카오", "카카오페이", "두나무", "삼성",
    "위믹스", "LG", "SK", "현대",
    "Hashed", "해시드", "크러스트", "클레이튼", "Klaytn", "위믹스", "Wemix",
    # 한국 언론사
    "코인데스크코리아", "디센터", "블록미디어", "토큰포스트",
    "뉴스1", "연합뉴스", "매일경제", "한국경제",
]

_kr_keywords_lower = [kw.lower() for kw in KR_KEYWORDS]


def is_korean_related(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in _kr_keywords_lower)


# === Coinness Telegram crawler ===

async def fetch_coinness_news(hours: int = 6) -> list:
    """Fetch recent messages from Coinness KR Telegram channel"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://t.me/s/coinnesskr")
            r.raise_for_status()
            html = r.text

        # Parse message texts
        text_pattern = re.compile(
            r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>',
            re.DOTALL
        )
        # Parse timestamps
        time_pattern = re.compile(
            r'<time[^>]*datetime="([^"]+)"'
        )

        texts = text_pattern.findall(html)
        times = time_pattern.findall(html)

        for i, raw_text in enumerate(texts):
            # Strip HTML tags
            clean = re.sub(r'<[^>]+>', '', raw_text).strip()
            if not clean:
                continue

            # Parse timestamp
            ts = None
            if i < len(times):
                try:
                    ts = datetime.fromisoformat(times[i].replace("+00:00", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except (ValueError, IndexError):
                    pass

            messages.append({
                "text": clean,
                "timestamp": ts.isoformat() if ts else None,
            })

        print(f"[KR-SENT] Coinness: fetched {len(messages)} messages (last {hours}h)")
        return messages

    except Exception as e:
        print(f"[KR-SENT] Coinness fetch failed: {e}")
        raise


# === Claude sentiment analysis ===

# Haiku 4.5 pricing: $1/MTok input, $5/MTok output
HAIKU_INPUT_COST_PER_MTOK = 1.0
HAIKU_OUTPUT_COST_PER_MTOK = 5.0

SENTIMENT_VALUES = ["BULLISH", "BEARISH", "NEUTRAL", "CAUTIOUS_FOMO", "PANIC", "GREED", "UNCERTAIN"]

SYSTEM_PROMPT = """You are a senior Korean crypto market analyst providing real-time sentiment analysis in English.
You combine Korean exchange data with Korean crypto news to produce actionable intelligence.
Your analysis targets professional traders and AI trading agents.
Always respond in English. Be specific and reference actual data points."""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
    )),
    reraise=True,
)
def _call_claude_with_retry(client, messages, system):
    return client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=system,
        messages=messages,
    )


def analyze_sentiment(exchange_data: dict, korean_news: list, all_news_count: int, coinness_status: str) -> tuple:
    """Call Claude Haiku to analyze sentiment. Returns (result_dict, cost_usd)."""

    news_section = ""
    if korean_news:
        news_items = []
        for n in korean_news[:15]:  # limit to 15 articles
            news_items.append(f"- [{n.get('timestamp', 'unknown')}] {n['text'][:200]}")
        news_section = f"""
Korean-related news ({len(korean_news)} articles from Coinness Telegram, last 6 hours):
{chr(10).join(news_items)}"""
    else:
        if coinness_status == "ok":
            news_section = "\nNo Korean-related news found in the last 6 hours."
        else:
            news_section = "\nKorean news source (Coinness) unavailable. Analysis based on exchange data only."

    # Build exchange summary
    intel_lines = []
    if exchange_data:
        premiums = exchange_data.get("premiums", [])
        if premiums:
            avg_prem = sum(p["premium_pct"] for p in premiums) / len(premiums)
            intel_lines.append(f"Average Kimchi Premium: {avg_prem:.2f}% across {len(premiums)} tokens")

            extreme_pos = [p for p in premiums if p["premium_pct"] > 5]
            extreme_neg = [p for p in premiums if p["premium_pct"] < -5]
            if extreme_pos:
                intel_lines.append(f"High premium tokens (>5%): {', '.join(p['symbol'] + ':' + str(p['premium_pct']) + '%' for p in extreme_pos[:5])}")
            if extreme_neg:
                intel_lines.append(f"Reverse premium tokens (<-5%): {', '.join(p['symbol'] + ':' + str(p['premium_pct']) + '%' for p in extreme_neg[:5])}")

        caution = exchange_data.get("caution_tokens", [])
        deposit_soaring = [c for c in caution if "DEPOSIT_SOARING" in c.get("flags", [])]
        volume_soaring = [c for c in caution if "VOLUME_SOARING" in c.get("flags", [])]
        warnings = [c for c in caution if "INVESTMENT_WARNING" in c.get("flags", [])]

        if deposit_soaring:
            intel_lines.append(f"Deposit soaring: {', '.join(c['symbol'] for c in deposit_soaring[:10])}")
        if volume_soaring:
            intel_lines.append(f"Volume soaring: {', '.join(c['symbol'] for c in volume_soaring[:10])}")
        if warnings:
            intel_lines.append(f"Investment warnings: {len(warnings)} tokens")

        movers = exchange_data.get("movers_1m", [])
        if movers:
            intel_lines.append(f"1-min movers: {', '.join(m['symbol'] + ':' + str(m['change_1m_pct']) + '%' for m in movers[:5])}")

        ms = exchange_data.get("market_share", {})
        if ms:
            intel_lines.append(f"Market share: Upbit {ms.get('upbit_pct', 0)}% / Bithumb {ms.get('bithumb_pct', 0)}%")

    exchange_section = "\n".join(intel_lines) if intel_lines else "Exchange data unavailable."

    user_msg = f"""Analyze the current Korean crypto market sentiment based on the following data:

EXCHANGE INTELLIGENCE (real-time, {exchange_data.get('common_symbols_count', 0)} tokens monitored):
{exchange_section}

NEWS CONTEXT:
{news_section}

Provide your analysis as JSON only (no markdown, no backticks):
{{"sentiment":"one of {SENTIMENT_VALUES}","score":0.0,"report_en":"3-4 sentence English analysis combining exchange data and news context. Be specific with numbers."}}

score range: -1.0 (extreme bearish) to +1.0 (extreme bullish). 0.0 = neutral."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30.0)
        response = _call_claude_with_retry(client, [{"role": "user", "content": user_msg}], SYSTEM_PROMPT)

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = (input_tokens * HAIKU_INPUT_COST_PER_MTOK + output_tokens * HAIKU_OUTPUT_COST_PER_MTOK) / 1_000_000

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        # Validate sentiment value
        if result.get("sentiment") not in SENTIMENT_VALUES:
            result["sentiment"] = "UNCERTAIN"
        # Clamp score
        score = float(result.get("score", 0))
        result["score"] = max(-1.0, min(1.0, score))

        print(f"[KR-SENT] Claude: in={input_tokens}t, out={output_tokens}t, cost=${cost:.6f}")
        log_event("claude_call", endpoint="kr-sentiment", cost_usd=cost,
                  tokens_in=input_tokens, tokens_out=output_tokens)

        return result, cost

    except json.JSONDecodeError as e:
        print(f"[KR-SENT] Claude JSON parse error: {e}")
        log_event("error", endpoint="kr-sentiment", error=f"json_parse: {e}")
        return {
            "sentiment": "UNCERTAIN",
            "score": 0.0,
            "report_en": "AI parsing error. Analysis based on raw exchange data.",
        }, 0.0

    except Exception as e:
        print(f"[KR-SENT] Claude API error: {e}")
        log_event("error", endpoint="kr-sentiment", error=f"claude_api: {str(e)[:100]}")
        raise


# === Cache + concurrency lock ===

_kr_sentiment_cache = {
    "data": None,
    "expires_at": 0,
    "lock": None,  # initialized lazily
}


def _get_lock():
    if _kr_sentiment_cache["lock"] is None:
        _kr_sentiment_cache["lock"] = asyncio.Lock()
    return _kr_sentiment_cache["lock"]


def save_cache_to_disk():
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "data": _kr_sentiment_cache["data"],
                "expires_at": _kr_sentiment_cache["expires_at"],
            }, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"[KR-SENT] Disk save failed: {e}")


def load_cache_from_disk():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                saved = json.load(f)
            if saved.get("expires_at", 0) > time.time():
                _kr_sentiment_cache["data"] = saved["data"]
                _kr_sentiment_cache["expires_at"] = saved["expires_at"]
                remaining = int(saved["expires_at"] - time.time())
                print(f"[KR-SENT] Cache restored from disk (expires in {remaining}s)")
            else:
                print("[KR-SENT] Disk cache expired, ignoring")
    except Exception as e:
        print(f"[KR-SENT] Disk load failed: {e}")


# === Anomaly detection ===

CLAUDE_HOURLY_LIMIT = 20
_claude_call_history = []
_last_anomaly_alert = {"ts": 0}


async def check_claude_anomaly(tg_send_func, cost: float):
    now = time.time()
    _claude_call_history.append((now, cost))
    _claude_call_history[:] = [x for x in _claude_call_history if now - x[0] < 3600]

    hourly_count = len(_claude_call_history)
    if hourly_count >= CLAUDE_HOURLY_LIMIT:
        hourly_cost = sum(x[1] for x in _claude_call_history)
        if now - _last_anomaly_alert["ts"] > 600:  # 10min cooldown
            _last_anomaly_alert["ts"] = now
            msg = (
                f"⚠️ Claude 호출 폭주 감지!\n"
                f"최근 1시간: {hourly_count}회, ${hourly_cost:.4f}\n"
                f"임계값: {CLAUDE_HOURLY_LIMIT}회/시간"
            )
            if tg_send_func:
                await tg_send_func(msg)
            log_event("anomaly_detected", type="claude_hourly_exceeded",
                      count=hourly_count, cost=hourly_cost)


# === Main handler ===

async def handle_kr_sentiment(tg_send_func=None):
    """Main kr-sentiment handler with 1-hour cache + concurrency lock"""
    now = time.time()

    # Fast path: cache hit (no lock needed)
    if _kr_sentiment_cache["data"] and now < _kr_sentiment_cache["expires_at"]:
        data = dict(_kr_sentiment_cache["data"])
        data["_meta"] = dict(data.get("_meta", {}))
        data["_meta"]["cache_age_seconds"] = int(now - data["_meta"].get("_computed_ts", now))
        log_event("cache_hit", endpoint="kr-sentiment")
        return data

    # Slow path: cache miss — acquire lock
    lock = _get_lock()
    async with lock:
        # Re-check after acquiring lock (another request may have filled cache)
        now = time.time()
        if _kr_sentiment_cache["data"] and now < _kr_sentiment_cache["expires_at"]:
            data = dict(_kr_sentiment_cache["data"])
            data["_meta"] = dict(data.get("_meta", {}))
            data["_meta"]["cache_age_seconds"] = int(now - data["_meta"].get("_computed_ts", now))
            log_event("cache_hit", endpoint="kr-sentiment")
            return data

        result = await _compute_kr_sentiment(tg_send_func)
        _kr_sentiment_cache["data"] = result
        _kr_sentiment_cache["expires_at"] = now + 3600  # 1 hour TTL
        save_cache_to_disk()
        return result


async def _compute_kr_sentiment(tg_send_func=None):
    """Compute fresh sentiment data"""
    # 1. Exchange data (required)
    intel = compute_intel_data()
    if not intel:
        raise Exception("Exchange data unavailable, retry in 1 minute")

    # 2. Coinness news (optional — graceful degradation)
    news = []
    coinness_status = "ok"
    try:
        news = await fetch_coinness_news(hours=6)
    except Exception as e:
        print(f"[KR-SENT] Coinness fetch failed, continuing without news: {e}")
        coinness_status = "failed"

    korean_news = [n for n in news if is_korean_related(n["text"])]

    # Classify news tone (simple rule-based for the response, Claude does deeper analysis)
    news_context = []
    for n in korean_news:
        news_context.append({
            "title": n["text"][:100],
            "timestamp": n.get("timestamp"),
        })

    # 3. Claude analysis (runs in executor to not block event loop)
    loop = asyncio.get_event_loop()
    analysis, cost = await loop.run_in_executor(
        None, analyze_sentiment, intel, korean_news, len(news), coinness_status
    )

    # 4. Anomaly check
    if cost > 0:
        await check_claude_anomaly(tg_send_func, cost)

    # 5. Build exchange signals summary
    premiums = intel.get("premiums", [])
    caution = intel.get("caution_tokens", [])
    avg_premium = sum(p["premium_pct"] for p in premiums) / len(premiums) if premiums else 0

    deposit_soaring = [c["symbol"] for c in caution if "DEPOSIT_SOARING" in c.get("flags", [])]
    volume_soaring_tokens = [c["symbol"] for c in caution if "VOLUME_SOARING" in c.get("flags", [])]
    warning_count = len([c for c in caution if "INVESTMENT_WARNING" in c.get("flags", [])])

    extreme_premiums = []
    for p in premiums:
        if abs(p["premium_pct"]) > 5:
            extreme_premiums.append({"symbol": p["symbol"], "premium_pct": p["premium_pct"]})

    # Sources
    upbit_count = len(intel.get("premiums", []))
    sources = [
        f"Upbit API ({upbit_count} tokens, real-time)",
        f"Bithumb API (real-time)",
        f"Binance API (reference)",
    ]
    if coinness_status == "ok":
        sources.append(f"Coinness Telegram ({len(news)} articles analyzed, {len(korean_news)} Korean-related)")
    else:
        sources.append("Coinness Telegram (unavailable)")

    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    result = {
        "sentiment": analysis.get("sentiment", "UNCERTAIN"),
        "score": analysis.get("score", 0.0),
        "report_en": analysis.get("report_en", "Analysis unavailable."),
        "exchange_signals": {
            "deposit_soaring": deposit_soaring[:10],
            "volume_soaring": volume_soaring_tokens[:10],
            "warnings": warning_count,
            "avg_premium_pct": round(avg_premium, 2),
            "extreme_premium_tokens": extreme_premiums[:10],
        },
        "news_context": {
            "korean_related": news_context[:10],
            "total_analyzed": len(news),
            "korean_count": len(korean_news),
            "news_freshness_hours": 6,
        },
        "sources": sources,
        "timestamp": computed_at,
        "_meta": {
            "cache_age_seconds": 0,
            "computed_at": computed_at,
            "_computed_ts": time.time(),
            "data_sources_status": {
                "exchange_intel": "ok",
                "coinness": coinness_status,
            },
        },
    }

    return result


# Load disk cache on module import
load_cache_from_disk()
print("[KR-SENT] kr_sentiment module loaded")
