import os
import json
import time
import asyncio
import httpx
from datetime import datetime, timezone, timedelta

from stats_logger import log_event, aggregate_stats, aggregate_stats_range

# ============================================================
# Korean Exchange Intelligence - Background Collector
# ============================================================

# 업비트→바이낸스 티커 매핑 (리브랜딩/이름 차이 대응)
# key=업비트 심볼, value=바이낸스 심볼
TICKER_MAP_UPBIT_TO_BINANCE = {
    # 필요시 추가: "UPBIT_SYMBOL": "BINANCE_SYMBOL"
}
# 바이낸스→업비트 역매핑 (자동 생성)
TICKER_MAP_BINANCE_TO_UPBIT = {v: k for k, v in TICKER_MAP_UPBIT_TO_BINANCE.items()}

def normalize_symbol_for_binance(upbit_sym):
    return TICKER_MAP_UPBIT_TO_BINANCE.get(upbit_sym, upbit_sym)

def normalize_symbol_for_upbit(binance_sym):
    return TICKER_MAP_BINANCE_TO_UPBIT.get(binance_sym, binance_sym)

# 김프 알림 쿨다운
_alert_cooldown = {}
tg_send_func = None

# 전역 저장소
intel_cache = {
    "upbit_tickers": {},
    "bithumb_tickers": {},
    "binance_tickers": {},
    "upbit_market_details": {},
    "prev_upbit_tickers": {},
    "prev_market_list": set(),
    "current_market_list": set(),
    "common_symbols": [],
    "fx_rate": 0,
    "last_update": 0,
}

ALERT_HISTORY_FILE = os.getenv("ALERT_HISTORY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_history.json"))

def load_alert_history():
    try:
        with open(ALERT_HISTORY_FILE) as f:
            return json.load(f)
    except:
        return []

def save_alert_history(alerts):
    try:
        with open(ALERT_HISTORY_FILE, "w") as f:
            json.dump(alerts[-500:], f)
    except:
        pass

async def fetch_all_upbit_tickers():
    """업비트 전종목 KRW 마켓 ticker"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            # 먼저 KRW 마켓 목록
            r = await c.get("https://api.upbit.com/v1/market/all")
            markets = [m["market"] for m in r.json() if m["market"].startswith("KRW-")]
            market_str = ",".join(markets)
            # ticker 조회
            r2 = await c.get(f"https://api.upbit.com/v1/ticker?markets={market_str}")
            result = {}
            for t in r2.json():
                sym = t["market"].replace("KRW-", "")
                result[sym] = {
                    "price_krw": t["trade_price"],
                    "volume_24h": t["acc_trade_price_24h"],
                    "change_rate": t.get("signed_change_rate", 0),
                    "change_price": t.get("signed_change_price", 0),
                    "high_price": t["high_price"],
                    "low_price": t["low_price"],
                    "timestamp": t["timestamp"],
                }
            return result
    except Exception as e:
        print(f"[INTEL] upbit ticker error: {e}")
        return {}

async def fetch_all_bithumb_tickers():
    """빗썸 전종목 KRW ticker"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.bithumb.com/public/ticker/ALL_KRW")
            data = r.json().get("data", {})
            result = {}
            for sym, info in data.items():
                if sym == "date":
                    continue
                try:
                    result[sym] = {
                        "price_krw": float(info["closing_price"]),
                        "volume_24h": float(info.get("acc_trade_value_24H", 0)),
                        "change_rate": float(info.get("fluctate_rate_24H", 0)) / 100,
                        "high_price": float(info.get("max_price", 0)),
                        "low_price": float(info.get("min_price", 0)),
                    }
                except:
                    continue
            return result
    except Exception as e:
        print(f"[INTEL] bithumb ticker error: {e}")
        return {}

async def fetch_all_binance_tickers():
    """바이낸스 전종목 USDT ticker"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.binance.com/api/v3/ticker/24hr")
            result = {}
            for t in r.json():
                if t["symbol"].endswith("USDT"):
                    sym = t["symbol"].replace("USDT", "")
                    try:
                        result[sym] = {
                            "price_usdt": float(t["lastPrice"]),
                            "volume_24h_usdt": float(t["quoteVolume"]),
                            "change_pct": float(t["priceChangePercent"]),
                        }
                    except:
                        continue
            return result
    except Exception as e:
        print(f"[INTEL] binance ticker error: {e}")
        return {}

async def fetch_upbit_market_details():
    """업비트 전종목 마켓 상세 (유의종목/투자경고/이벤트 플래그)"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.upbit.com/v1/market/all?is_details=true")
            result = {}
            krw_markets = set()
            for m in r.json():
                if not m["market"].startswith("KRW-"):
                    continue
                sym = m["market"].replace("KRW-", "")
                krw_markets.add(sym)
                evt = m.get("market_event", {})
                caution = evt.get("caution", {})
                result[sym] = {
                    "korean_name": m.get("korean_name", ""),
                    "english_name": m.get("english_name", ""),
                    "warning": evt.get("warning", False),
                    "caution_price_fluctuations": caution.get("PRICE_FLUCTUATIONS", False),
                    "caution_volume_soaring": caution.get("TRADING_VOLUME_SOARING", False),
                    "caution_deposit_soaring": caution.get("DEPOSIT_AMOUNT_SOARING", False),
                    "caution_global_price_diff": caution.get("GLOBAL_PRICE_DIFFERENCES", False),
                    "caution_small_accounts": caution.get("CONCENTRATION_OF_SMALL_ACCOUNTS", False),
                }
            return result, krw_markets
    except Exception as e:
        print(f"[INTEL] upbit market detail error: {e}")
        return {}, set()





def compute_intel_data():
    """캐시된 원본 데이터로 모든 인텔리전스 계산"""
    c = intel_cache
    fx = c["fx_rate"]
    if not fx or not c["upbit_tickers"] or not c["binance_tickers"]:
        return None

    upbit = c["upbit_tickers"]
    bithumb = c["bithumb_tickers"]
    binance = c["binance_tickers"]
    prev_upbit = c["prev_upbit_tickers"]
    details = c["upbit_market_details"]

    # === 공통 심볼 (업비트-바이낸스, 매핑 포함) ===
    # 직접 매칭
    common = sorted(set(upbit.keys()) & set(binance.keys()))
    # 매핑을 통한 추가 매칭
    mapped_pairs = {}  # upbit_sym -> binance_sym
    for u_sym in upbit:
        b_sym = normalize_symbol_for_binance(u_sym)
        if b_sym != u_sym and b_sym in binance and u_sym not in common:
            mapped_pairs[u_sym] = b_sym
            common.append(u_sym)
    common = sorted(set(common))

    # === 1. 토큰별 김프 + 역김프 ===
    premiums = []
    for sym in common:
        u_krw = upbit[sym]["price_krw"]
        b_sym = mapped_pairs.get(sym, sym)
        b_usd = binance[b_sym]["price_usdt"]
        if b_usd <= 0:
            continue
        global_krw = b_usd * fx
        pct = ((u_krw - global_krw) / global_krw) * 100
        d = details.get(sym, {})
        premiums.append({
            "symbol": sym,
            "korean_name": d.get("korean_name", ""),
            "upbit_krw": u_krw,
            "binance_usd": b_usd,
            "global_krw": round(global_krw, 2),
            "premium_pct": round(pct, 3),
            "warning": d.get("warning", False),
            "caution_volume_soaring": d.get("caution_volume_soaring", False),
            "caution_deposit_soaring": d.get("caution_deposit_soaring", False),
            "caution_global_price_diff": d.get("caution_global_price_diff", False),
            "upbit_volume_krw": upbit[sym]["volume_24h"],
        })
    premiums.sort(key=lambda x: x["premium_pct"], reverse=True)

    # === 2. 업비트-빗썸 괴리 ===
    exchange_gaps = []
    common_domestic = sorted(set(upbit.keys()) & set(bithumb.keys()))
    for sym in common_domestic:
        u = upbit[sym]["price_krw"]
        b = bithumb[sym]["price_krw"]
        if b <= 0:
            continue
        gap = ((u - b) / b) * 100
        if abs(gap) > 0.3:  # 0.3% 이상만
            exchange_gaps.append({
                "symbol": sym,
                "upbit_krw": u,
                "bithumb_krw": b,
                "gap_pct": round(gap, 3),
                "upbit_vol": upbit[sym]["volume_24h"],
                "bithumb_vol": bithumb[sym]["volume_24h"],
            })
    exchange_gaps.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)

    # === 3. 거래대금 TOP 20 ===
    top_volume = sorted(
        [{"symbol": s, "volume_krw": d["volume_24h"], "change_rate": d["change_rate"]} for s, d in upbit.items()],
        key=lambda x: x["volume_krw"], reverse=True
    )[:20]

    # === 4. 급등/급락 감지 (1분 전 대비) ===
    movers = []
    if prev_upbit:
        for sym in upbit:
            if sym in prev_upbit:
                curr = upbit[sym]["price_krw"]
                prev = prev_upbit[sym]["price_krw"]
                if prev <= 0:
                    continue
                chg = ((curr - prev) / prev) * 100
                if abs(chg) > 1.0:  # 1분간 1% 이상 변동
                    movers.append({
                        "symbol": sym,
                        "prev_price": prev,
                        "curr_price": curr,
                        "change_1m_pct": round(chg, 3),
                        "volume_krw": upbit[sym]["volume_24h"],
                    })
    movers.sort(key=lambda x: abs(x["change_1m_pct"]), reverse=True)

    # === 5. 거래량 급등 (24h 변화율 상위) ===
    vol_spikes = sorted(
        [{"symbol": s, "volume_krw": d["volume_24h"], "change_rate_24h": d["change_rate"]}
         for s, d in upbit.items() if d["volume_24h"] > 1_000_000_000],  # 10억원 이상만
        key=lambda x: abs(x["change_rate_24h"]), reverse=True
    )[:20]

    # === 6. 신규 상장/상폐 감지 (마켓 리스트 비교) ===
    listing_changes = []
    prev_markets = c["prev_market_list"]
    curr_markets = c["current_market_list"]
    if prev_markets:
        new_listings = curr_markets - prev_markets
        delistings = prev_markets - curr_markets
        for sym in new_listings:
            listing_changes.append({
                "symbol": sym,
                "type": "NEW_LISTING",
                "korean_name": details.get(sym, {}).get("korean_name", ""),
                "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        for sym in delistings:
            listing_changes.append({
                "symbol": sym,
                "type": "DELISTING",
                "detected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })

    # === 유의/경고 종목 ===
    caution_tokens = []
    for sym, d in details.items():
        flags = []
        if d.get("warning"): flags.append("INVESTMENT_WARNING")
        if d.get("caution_price_fluctuations"): flags.append("PRICE_FLUCTUATIONS")
        if d.get("caution_volume_soaring"): flags.append("VOLUME_SOARING")
        if d.get("caution_deposit_soaring"): flags.append("DEPOSIT_SOARING")
        if d.get("caution_global_price_diff"): flags.append("GLOBAL_PRICE_DIFF")
        if d.get("caution_small_accounts"): flags.append("SMALL_ACCOUNTS_CONCENTRATION")
        if flags:
            caution_tokens.append({
                "symbol": sym,
                "korean_name": d.get("korean_name", ""),
                "flags": flags,
            })

    # === 7. 거래소 점유율 (업비트 vs 빗썸) ===
    total_upbit = sum(d["volume_24h"] for d in upbit.values())
    total_bithumb = sum(d["volume_24h"] for d in bithumb.values())
    total = total_upbit + total_bithumb
    market_share = {
        "upbit_pct": round(total_upbit / total * 100, 1) if total > 0 else 0,
        "bithumb_pct": round(total_bithumb / total * 100, 1) if total > 0 else 0,
        "upbit_volume_krw": total_upbit,
        "bithumb_volume_krw": total_bithumb,
    }

    return {
        "premiums": premiums,
        "exchange_gaps": exchange_gaps[:20],
        "top_volume": top_volume,
        "movers_1m": movers[:20],
        "vol_spikes": vol_spikes,
        "listing_changes": listing_changes,
        "caution_tokens": caution_tokens,
        "market_share": market_share,
        "common_symbols_count": len(common),
        "fx_rate": fx,
        "last_update": c["last_update"],
    }

async def intel_polling_task(fetch_fx_func, tg_func=None):
    global tg_send_func
    tg_send_func = tg_func
    """1분마다 전종목 데이터 수집"""
    while True:
        try:
            # 이전 캐시 보존
            intel_cache["prev_upbit_tickers"] = dict(intel_cache["upbit_tickers"])
            intel_cache["prev_market_list"] = set(intel_cache["current_market_list"])

            # 병렬 수집
            upbit, bithumb, binance, market_detail_result, fx_data = await asyncio.gather(
                fetch_all_upbit_tickers(),
                fetch_all_bithumb_tickers(),
                fetch_all_binance_tickers(),
                fetch_upbit_market_details(),
                fetch_fx_func(),
                return_exceptions=True,
            )

            if isinstance(upbit, dict) and upbit:
                intel_cache["upbit_tickers"] = upbit
            if isinstance(bithumb, dict) and bithumb:
                intel_cache["bithumb_tickers"] = bithumb
            if isinstance(binance, dict) and binance:
                intel_cache["binance_tickers"] = binance
            if isinstance(market_detail_result, tuple):
                details, market_set = market_detail_result
                intel_cache["upbit_market_details"] = details
                intel_cache["current_market_list"] = market_set
            if isinstance(fx_data, dict) and fx_data:
                intel_cache["fx_rate"] = fx_data.get("rate", intel_cache["fx_rate"])
            elif isinstance(fx_data, (int, float)):
                intel_cache["fx_rate"] = fx_data



            intel_cache["last_update"] = time.time()
            intel_cache["common_symbols"] = sorted(set(intel_cache["upbit_tickers"].keys()) & set(intel_cache["binance_tickers"].keys()))

            count = len(intel_cache["upbit_tickers"])
            print(f"[INTEL] Updated: {count} upbit, {len(intel_cache['bithumb_tickers'])} bithumb, {len(intel_cache['binance_tickers'])} binance, FX={intel_cache['fx_rate']}")

        except Exception as e:
            print(f"[INTEL] polling error: {e}")

        await asyncio.sleep(60)

# 텔레그램 /kimp 명령어 핸들러
_tg_last_update_id = 0

async def tg_bot_polling(tg_token, tg_chat):
    """텔레그램 봇 메시지 폴링 — /kimp, /sentiment, /stats, /cost 명령어 처리"""
    global _tg_last_update_id
    if not tg_token or not tg_chat:
        return
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{tg_token}/getUpdates",
                    params={"offset": _tg_last_update_id + 1, "timeout": 30}
                )
                updates = r.json().get("result", [])
                for u in updates:
                    _tg_last_update_id = u["update_id"]
                    msg = u.get("message", {})
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != tg_chat:
                        continue
                    if text == "/kimp":
                        await handle_kimp_command(tg_token, tg_chat)
                    elif text == "/sentiment":
                        await handle_sentiment_command(tg_token, tg_chat)
                    elif text == "/stats":
                        await handle_stats_command(tg_token, tg_chat)
                    elif text == "/cost":
                        await handle_cost_command(tg_token, tg_chat)
        except Exception as e:
            print(f"[TG-POLL] error: {type(e).__name__}: {e}")
        await asyncio.sleep(5)

async def handle_kimp_command(tg_token, tg_chat):
    """현재 김프 10% 이상 토큰 + 이전 알림 토큰 상태 응답"""
    intel = compute_intel_data()
    if not intel:
        await _tg_reply(tg_token, tg_chat, "❌ 데이터 수집 중. 1분 후 재시도.")
        return

    # 현재 10% 이상
    extreme = [p for p in intel.get("premiums", []) if abs(p["premium_pct"]) >= 10.0]
    extreme.sort(key=lambda x: abs(x["premium_pct"]), reverse=True)

    if not extreme:
        msg = "✅ 현재 김프 ±10% 이상 토큰 없음"
    else:
        lines = []
        for p in extreme[:15]:
            direction = "🔴" if p["premium_pct"] < 0 else "🟢"
            flags = []
            if p.get("warning"): flags.append("⛔경고")
            if p.get("caution_volume_soaring"): flags.append("📈거래량↑")
            if p.get("caution_deposit_soaring"): flags.append("💰입금↑")
            flag_str = " ".join(flags)
            lines.append(f"{direction} {p['symbol']}: {p['premium_pct']}% {flag_str}")
        msg = f"📊 김프 이상치 ({len(extreme)}개)\n\n" + "\n".join(lines)

    # 이전 알림 토큰 현재 상태
    if _alert_cooldown:
        tracked = []
        for sym in _alert_cooldown:
            current = next((p for p in intel.get("premiums", []) if p["symbol"] == sym), None)
            if current:
                tracked.append(f"  {sym}: {current['premium_pct']}%")
        if tracked:
            msg += "\n\n📌 이전 알림 토큰 현재 상태:\n" + "\n".join(tracked)

    msg += f"\n\n🕐 {time.strftime('%H:%M:%S KST', time.localtime(time.time() + 32400))}"
    await _tg_reply(tg_token, tg_chat, msg)

async def handle_sentiment_command(tg_token, tg_chat):
    """텔레그램 /sentiment — 캐시 활용 즉시 응답"""
    try:
        from kr_sentiment import handle_kr_sentiment
        result = await handle_kr_sentiment(tg_send_func=None)

        sentiment = result.get("sentiment", "UNKNOWN")
        score = result.get("score", 0)
        report = result.get("report_en", "N/A")
        es = result.get("exchange_signals", {})
        nc = result.get("news_context", {})
        meta = result.get("_meta", {})
        cache_age = meta.get("cache_age_seconds", 0)

        # Sentiment emoji
        emoji_map = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪", "CAUTIOUS_FOMO": "🟡",
                     "PANIC": "🔴", "GREED": "🟢", "UNCERTAIN": "❓"}
        emoji = emoji_map.get(sentiment, "❓")

        msg = (
            f"{emoji} <b>KR Sentiment</b>: {sentiment} ({score:+.2f})\n\n"
            f"{report}\n\n"
            f"📊 Exchange: 김프 avg {es.get('avg_premium_pct', 0)}%, 경고 {es.get('warnings', 0)}개\n"
        )
        if es.get("deposit_soaring"):
            msg += f"💰 입금급등: {', '.join(es['deposit_soaring'][:5])}\n"
        if es.get("volume_soaring"):
            msg += f"📈 거래량급등: {', '.join(es['volume_soaring'][:5])}\n"
        msg += (
            f"\n📰 뉴스: {nc.get('korean_count', 0)}/{nc.get('total_analyzed', 0)}건 한국관련\n"
            f"🕐 캐시: {cache_age}s전 | {time.strftime('%H:%M KST', time.localtime(time.time() + 32400))}"
        )
        await _tg_reply(tg_token, tg_chat, msg)
    except Exception as e:
        await _tg_reply(tg_token, tg_chat, f"❌ Sentiment 오류: {str(e)[:100]}")


def calculate_cdp_cost(monthly_paid_calls: int) -> tuple:
    """월 결제 건수로 CDP facilitator 비용 + 무료 한도 사용량 추정.
    Coinbase 정책: 월 1000건 무료, 초과분 $0.001/건.

    Returns: (cost_usd, free_tier_used, free_tier_remaining)
    """
    FREE_TIER = 1000
    COST_PER_CALL = 0.001
    used = min(monthly_paid_calls, FREE_TIER)
    remaining = max(0, FREE_TIER - monthly_paid_calls)
    cost = max(0, monthly_paid_calls - FREE_TIER) * COST_PER_CALL
    return cost, used, remaining


def _aggregate_paid_breakdown(start_ts: int, end_ts: int = None) -> dict:
    """stats.jsonl을 직접 파싱해서 IP별/endpoint별 매출 그룹.
    aggregate_stats_range는 ip 그룹을 안 해주므로 별도 집계."""
    from stats_logger import STATS_FILE
    import os as _os

    by_ip = {}        # ip -> {"calls": n, "revenue": usd}
    by_endpoint = {}  # endpoint -> {"calls": n, "revenue": usd}
    paid_total = 0
    revenue_total = 0.0
    real_user_ips = set()  # owner 제외 + paid_calls 1+ 인 IP 집합

    # 본인 검증 IP — main.py 와 동일하게 정의 (lazy import 회피용 hardcode)
    OWNER_IPS = {"118.40.115.95", "1.249.16.154"}

    if not _os.path.exists(STATS_FILE):
        return {
            "by_ip": by_ip, "by_endpoint": by_endpoint,
            "paid_total": paid_total, "revenue_total": revenue_total,
            "real_user_count": 0,
        }

    try:
        with open(STATS_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = e.get("ts", 0)
                if ts < start_ts:
                    continue
                if end_ts is not None and ts >= end_ts:
                    continue
                if e.get("type") != "api_call" or not e.get("paid"):
                    continue

                price = e.get("price_usd", 0) or 0
                paid_total += 1
                revenue_total += price

                ip = e.get("ip") or "unknown"
                ep = e.get("endpoint") or "unknown"
                by_ip.setdefault(ip, {"calls": 0, "revenue": 0.0})
                by_ip[ip]["calls"] += 1
                by_ip[ip]["revenue"] += price
                by_endpoint.setdefault(ep, {"calls": 0, "revenue": 0.0})
                by_endpoint[ep]["calls"] += 1
                by_endpoint[ep]["revenue"] += price

                if ip and ip != "unknown" and ip not in OWNER_IPS:
                    real_user_ips.add(ip)
    except Exception as e:
        print(f"[STATS-BREAKDOWN] read error: {e}")

    return {
        "by_ip": by_ip,
        "by_endpoint": by_endpoint,
        "paid_total": paid_total,
        "revenue_total": round(revenue_total, 4),
        "real_user_count": len(real_user_ips),
    }


def _aggregate_settled_breakdown(start_ts: int, end_ts: int = None) -> dict:
    """payment_settled 이벤트를 읽어 network 별 / Solana payer 별 매출 집계.
    이 이벤트는 main.py rate_limit_middleware → tg_notify_request 에서만 기록되므로
    network/payer 정보를 자동으로 갖춤. 신규 결제만 집계됨 (legacy api_call은 network 없음)."""
    from stats_logger import STATS_FILE
    import os as _os

    by_network = {}      # network_label -> {"calls": n, "revenue": usd}
    solana_payers = {}   # payer addr -> {"calls": n, "revenue": usd, "endpoints": Counter-like dict}
    settled_total = 0
    settled_revenue = 0.0

    if not _os.path.exists(STATS_FILE):
        return {"by_network": by_network, "solana_payers": solana_payers,
                "settled_total": settled_total, "settled_revenue": settled_revenue}

    try:
        with open(STATS_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if e.get("type") != "payment_settled":
                    continue
                ts = e.get("ts", 0)
                if ts < start_ts:
                    continue
                if end_ts is not None and ts >= end_ts:
                    continue

                price = e.get("price_usd", 0) or 0
                settled_total += 1
                settled_revenue += price

                net_label = e.get("network_label") or "unknown"
                by_network.setdefault(net_label, {"calls": 0, "revenue": 0.0})
                by_network[net_label]["calls"] += 1
                by_network[net_label]["revenue"] += price

                # Solana payer 별 그룹
                network = e.get("network") or ""
                if network.startswith("solana:"):
                    payer = e.get("payer") or "unknown"
                    ep = e.get("endpoint") or "unknown"
                    solana_payers.setdefault(payer, {"calls": 0, "revenue": 0.0, "endpoints": {}})
                    solana_payers[payer]["calls"] += 1
                    solana_payers[payer]["revenue"] += price
                    solana_payers[payer]["endpoints"].setdefault(ep, 0)
                    solana_payers[payer]["endpoints"][ep] += 1
    except Exception as e:
        print(f"[STATS-SETTLED] read error: {e}")

    return {
        "by_network": by_network,
        "solana_payers": solana_payers,
        "settled_total": settled_total,
        "settled_revenue": round(settled_revenue, 4),
    }


def _next_month_first(now_kst: datetime) -> datetime:
    """다음 달 1일 00:00 KST."""
    if now_kst.month == 12:
        return now_kst.replace(year=now_kst.year + 1, month=1, day=1,
                               hour=0, minute=0, second=0, microsecond=0)
    return now_kst.replace(month=now_kst.month + 1, day=1,
                           hour=0, minute=0, second=0, microsecond=0)


async def handle_stats_command(tg_token, tg_chat):
    """텔레그램 /stats — 오늘/이번달 통계 + CDP 비용/무료한도 + Top IP/endpoint + 진성 사용자"""
    try:
        now_kst = datetime.now(timezone(timedelta(hours=9)))

        # 오늘 (KST 기준)
        today_start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
        today_ts = int(today_start_kst.timestamp())
        today = aggregate_stats(today_ts)
        today_breakdown = _aggregate_paid_breakdown(today_ts)

        # 이번주 (월요일 시작) — 진성 사용자 카운트용
        weekday = now_kst.weekday()
        week_start = (now_kst - timedelta(days=weekday)).replace(hour=0, minute=0, second=0, microsecond=0)
        week_ts = int(week_start.timestamp())
        week_breakdown = _aggregate_paid_breakdown(week_ts)

        # 이번달 (KST 1일 00:00 ~ 현재)
        month_start_kst = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_ts = int(month_start_kst.timestamp())
        month = aggregate_stats(month_ts)
        month_breakdown = _aggregate_paid_breakdown(month_ts)

        # 이번달 network 분포 + Solana payer 분포 (payment_settled 이벤트 기반)
        month_settled = _aggregate_settled_breakdown(month_ts)

        # CDP 비용 + 무료 한도
        cdp_cost, cdp_used, cdp_remaining = calculate_cdp_cost(month["paid_calls"])

        # 진짜 순이익 = 매출 - Claude비용 - CDP비용
        true_profit = month["revenue_usd"] - month["claude_cost_usd"] - cdp_cost

        # Top endpoint (오늘) — 매출 기준
        today_eps_sorted = sorted(
            today_breakdown["by_endpoint"].items(),
            key=lambda kv: -kv[1]["revenue"]
        )
        top_today_ep_line = "—"
        if today_eps_sorted:
            ep, info = today_eps_sorted[0]
            pct = (info["revenue"] / today_breakdown["revenue_total"] * 100) if today_breakdown["revenue_total"] > 0 else 0
            top_today_ep_line = f"{ep} (${info['revenue']:.4f}, {pct:.0f}%)"

        # Top IP (이번달) — 매출 기준 1~3위
        month_ips_sorted = sorted(
            month_breakdown["by_ip"].items(),
            key=lambda kv: -kv[1]["revenue"]
        )
        ip_lines = []
        for ip, info in month_ips_sorted[:3]:
            pct = (info["revenue"] / month_breakdown["revenue_total"] * 100) if month_breakdown["revenue_total"] > 0 else 0
            ip_lines.append(f"• {ip}: ${info['revenue']:.4f} ({pct:.0f}%)")
        # 기타
        if len(month_ips_sorted) > 3:
            other_revenue = sum(info["revenue"] for _, info in month_ips_sorted[3:])
            other_pct = (other_revenue / month_breakdown["revenue_total"] * 100) if month_breakdown["revenue_total"] > 0 else 0
            ip_lines.append(f"• 기타 ({len(month_ips_sorted) - 3}개): ${other_revenue:.4f} ({other_pct:.0f}%)")

        # 다음 reset 일자
        reset_date = _next_month_first(now_kst).strftime("%-m/%-d")

        # CDP 무료 한도 라인
        if month["paid_calls"] > 1000:
            over = month["paid_calls"] - 1000
            cdp_line = f"사용: {month['paid_calls']:,} / 1,000건 ({over:,}건 초과)"
        else:
            cdp_line = f"사용: {month['paid_calls']:,} / 1,000건 (잔여 {cdp_remaining:,}건)"

        # CDP 비용 라인
        if cdp_cost > 0:
            over = month["paid_calls"] - 1000
            cdp_cost_line = f"CDP 비용: ${cdp_cost:.4f} ({month['paid_calls']:,}건 - 1,000건 = {over:,}건 초과)"
        else:
            cdp_cost_line = f"CDP 비용: $0.0000 (무료 한도 내)"

        msg = (
            f"📊 <b>KR Crypto Intelligence 통계</b>\n\n"
            f"─── 오늘 ({now_kst.strftime('%-m/%-d')} KST) ───\n"
            f"API 호출: {today['api_calls_total']}건 "
            f"(HIT {today['cache_hits']}, MISS {today['api_calls_total'] - today['cache_hits']}, "
            f"에러 {today['errors']}건)\n"
            f"유료 결제: {today['paid_calls']}건 / ${today['revenue_usd']:.4f}\n"
            f"Top endpoint: {top_today_ep_line}\n\n"
            f"─── 이번달 ({now_kst.strftime('%-m')}월) ───\n"
            f"매출: ${month['revenue_usd']:.4f}\n"
            f"Claude 비용: ${month['claude_cost_usd']:.6f}\n"
            f"{cdp_cost_line}\n"
            f"💰 순이익: ${true_profit:.4f}\n\n"
            f"─── CDP 무료 한도 ───\n"
            f"{cdp_line}\n"
            f"다음 reset: {reset_date}\n\n"
            f"─── Top IP (이번달, 매출 기준) ───\n"
            + ("\n".join(ip_lines) if ip_lines else "• 데이터 없음")
            + "\n\n"
            f"─── 네트워크별 매출 (이번달) ───\n"
        )

        # 네트워크 분포 — payment_settled 기반. 신규 결제만 포함되므로 settled_revenue 기준 %.
        network_lines = []
        if month_settled["settled_total"] > 0:
            net_sorted = sorted(
                month_settled["by_network"].items(),
                key=lambda kv: -kv[1]["revenue"]
            )
            for net_label, info in net_sorted:
                pct = (info["revenue"] / month_settled["settled_revenue"] * 100) if month_settled["settled_revenue"] > 0 else 0
                network_lines.append(f"• {net_label}: ${info['revenue']:.4f} ({info['calls']}건, {pct:.0f}%)")
            # legacy api_call (network 정보 없는 옛 결제)
            legacy_revenue = month["revenue_usd"] - month_settled["settled_revenue"]
            if legacy_revenue > 0.0001:
                network_lines.append(f"• unknown (legacy): ${legacy_revenue:.4f} (network 추적 전 결제)")
            msg += "\n".join(network_lines) + "\n\n"
        else:
            msg += "• payment_settled 이벤트 없음 (신규 결제 발생 시 자동 집계)\n\n"

        # Solana payer top 3
        if month_settled["solana_payers"]:
            msg += "─── Top Solana Wallets (이번달) ───\n"
            sol_sorted = sorted(
                month_settled["solana_payers"].items(),
                key=lambda kv: -kv[1]["revenue"]
            )
            for payer, info in sol_sorted[:3]:
                short = payer[:8] + "..." + payer[-6:] if len(payer) > 16 else payer
                top_ep = max(info["endpoints"].items(), key=lambda x: x[1])[0] if info["endpoints"] else "?"
                msg += f"• {short}: ${info['revenue']:.4f} ({info['calls']}건, {top_ep} 위주)\n"
            msg += "\n"

        msg += (
            f"─── 진성 사용자 (Owner 제외) ───\n"
            f"오늘: {today_breakdown['real_user_count']}명\n"
            f"이번주: {week_breakdown['real_user_count']}명\n"
            f"이번달: {month_breakdown['real_user_count']}명\n\n"
            f"🕐 {now_kst.strftime('%Y-%m-%d %H:%M KST')}"
        )
        await _tg_reply(tg_token, tg_chat, msg)
    except Exception as e:
        await _tg_reply(tg_token, tg_chat, f"❌ Stats 오류: {str(e)[:200]}")


async def handle_cost_command(tg_token, tg_chat):
    """텔레그램 /cost — Claude API 비용 상세"""
    try:
        now_kst = datetime.now(timezone(timedelta(hours=9)))
        today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
        today_ts = int(today_start.timestamp())
        today = aggregate_stats(today_ts)

        month_start = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_ts = int(month_start.timestamp())
        month = aggregate_stats(month_ts)

        msg = (
            f"💳 <b>Claude API 비용 상세</b>\n\n"
            f"─── 오늘 ───\n"
            f"Claude 호출: {today['claude_calls']}회\n"
            f"Claude 비용: ${today['claude_cost_usd']:.6f}\n\n"
            f"─── 이번달 ───\n"
            f"Claude 호출: {month['claude_calls']}회\n"
            f"Claude 비용: ${month['claude_cost_usd']:.6f}\n\n"
            f"─── 엔드포인트별 ───\n"
        )
        ep_data = month.get("by_endpoint", {})
        for ep, data in sorted(ep_data.items(), key=lambda x: x[1].get("cost", 0), reverse=True):
            if data.get("claude", 0) > 0:
                msg += f"  {ep}: {data['claude']}회, ${data['cost']:.6f}\n"

        msg += f"\n🕐 {now_kst.strftime('%Y-%m-%d %H:%M KST')}"
        await _tg_reply(tg_token, tg_chat, msg)
    except Exception as e:
        await _tg_reply(tg_token, tg_chat, f"❌ Cost 오류: {str(e)[:100]}")


async def _tg_reply(tg_token, tg_chat, text):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                         json={"chat_id": tg_chat, "text": text, "parse_mode": "HTML"})
    except Exception:
        pass

print("[PATCH] exchange_intel module loaded")
