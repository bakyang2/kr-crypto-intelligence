import json
import time
import asyncio
import httpx

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

ALERT_HISTORY_FILE = "/home/ubuntu/KRCryptoAPI/alert_history.json"

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

async def intel_polling_task(fetch_fx_func):
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

print("[PATCH] exchange_intel module loaded")
