"""
krw_macro.py — KRW Macro Stress Score (Phase 1)

Independent module, mirrors kr_news / kr_sentiment patterns.
Route wiring is deferred to main.py (Step 2 of Phase 1).

Score = 0..100 (higher = more KRW-side stress).
Method = rolling percentile 120d over a 2-year backfill.
Weights = us_rate 25 / risk_sentiment 25 / foreign_flow 20 / fx_momentum 20 / semiconductor 10.

Data sources:
  - FRED DGS3 (US 3Y treasury)
  - yfinance ^VIX / USDKRW=X / 000660.KS / 005930.KS
  - Naver chart JSON (외국인소진율 for 000660, 005930)
Fallbacks:
  - USDKRW=X yfinance → exchangerate-api.com if the primary fails

Never bootstraps on operational request path. Backfill runs on module import (once)
or restored from disk snapshot. Requests only read latest + cache.
"""
import asyncio
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

try:
    import anthropic
    _AI_CLIENT = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""), timeout=15.0)
except Exception:
    _AI_CLIENT = None

# ── constants
CACHE_TTL = 900          # 15 min
BACKFILL_YEARS_DAYS = 760  # ~2yr calendar
ROLLING_WINDOW = 120
WEIGHTS = {"us_rate_stress": 0.25, "risk_sentiment": 0.25, "foreign_flow": 0.20,
           "fx_momentum": 0.20, "semiconductor": 0.10}
STRUCTURAL_FAIL_THRESHOLD = 5   # consecutive fails → mark component degraded
SS_SHARES = 5_970_000_000
SK_SHARES = 728_000_000

FRED_KEY = os.getenv("FRED_API_KEY", "")

SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "krw_macro_cache.json")

# ── in-memory state
_state = {
    "history": None,       # pandas DataFrame indexed by date, cols: us3y, vix, usdkrw, sk, ss, fo_sk, fo_ss
    "result": None,        # last computed API response
    "expires_at": 0.0,
    "consecutive_fails": {},  # source_name → count
    "lock": None,
    "last_backfill_ok": None,
}

# ═════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ═════════════════════════════════════════════════════════════════

def _fetch_fred_dgs3(start_yyyymmdd, end_yyyymmdd):
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY missing")
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id=DGS3"
           f"&api_key={FRED_KEY}&file_type=json"
           f"&observation_start={start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:]}"
           f"&observation_end={end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:]}")
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read())
    rows = [(pd.Timestamp(o["date"]), float(o["value"]))
            for o in d.get("observations", []) if o.get("value") not in (".", "", None)]
    return pd.Series(dict(rows), name="us3y").sort_index()


def _fetch_naver_foreign(code, start_yyyymmdd, end_yyyymmdd):
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}"
           f"&requestType=1&startTime={start_yyyymmdd}&endTime={end_yyyymmdd}&timeframe=day")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode(errors="replace")
    txt = raw.replace("'", '"')
    txt = re.sub(r"\s+", " ", txt)
    txt = re.sub(r",\s*]", "]", txt)
    arr = json.loads(txt)
    header = arr[0]
    fo_idx = next(i for i, h in enumerate(header) if "외국인" in h)
    cl_idx = next(i for i, h in enumerate(header) if h == "종가")
    rows = arr[1:]
    fo = {pd.Timestamp(r[0]): float(r[fo_idx]) for r in rows if r[fo_idx] is not None}
    cl = {pd.Timestamp(r[0]): float(r[cl_idx]) for r in rows if r[cl_idx] is not None}
    return (pd.Series(fo, name=f"fo_{code}").sort_index(),
            pd.Series(cl, name=f"close_{code}").sort_index())


def _fetch_yf(sym, name, period="2y"):
    df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"yfinance empty for {sym}")
    s = df["Close"].dropna()
    s.index = s.index.tz_localize(None).normalize()
    s.name = name
    return s


def _fetch_usdkrw_fallback():
    """exchangerate-api fallback if yfinance USDKRW=X fails.
    Returns a single-point series with today's date."""
    url = "https://api.exchangerate-api.com/v4/latest/USD"
    with urllib.request.urlopen(url, timeout=8) as r:
        d = json.loads(r.read())
    krw = d.get("rates", {}).get("KRW")
    if krw is None:
        raise RuntimeError("exchangerate-api no KRW")
    ts = pd.Timestamp(d.get("date")).normalize()
    return pd.Series({ts: float(krw)}, name="usdkrw")


# ═════════════════════════════════════════════════════════════════
# BACKFILL / INCREMENTAL SYNC
# ═════════════════════════════════════════════════════════════════

def _empty_history():
    return pd.DataFrame(columns=["us3y", "vix", "usdkrw", "sk", "ss", "fo_sk", "fo_ss",
                                 "cl_sk", "cl_ss"])


def _full_backfill():
    """Build 2yr history from scratch. Raise on any HARD failure of a required source
    (this only runs at boot, not on a paid request)."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=BACKFILL_YEARS_DAYS)).strftime("%Y%m%d")

    us3y = _fetch_fred_dgs3(start, end)

    fo_sk, cl_sk = _fetch_naver_foreign("000660", start, end)
    fo_ss, cl_ss = _fetch_naver_foreign("005930", start, end)

    try:
        vix = _fetch_yf("^VIX", "vix")
    except Exception as e:
        raise RuntimeError(f"VIX backfill failed: {e}")

    try:
        usdkrw = _fetch_yf("USDKRW=X", "usdkrw")
    except Exception:
        usdkrw = _fetch_usdkrw_fallback()

    sk = _fetch_yf("000660.KS", "sk")
    ss = _fetch_yf("005930.KS", "ss")

    hist = pd.concat({
        "us3y": us3y, "vix": vix, "usdkrw": usdkrw,
        "sk": sk, "ss": ss,
        "fo_sk": fo_sk, "fo_ss": fo_ss,
        "cl_sk": cl_sk, "cl_ss": cl_ss,
    }, axis=1)
    # keep everything back to earliest we can; downstream aligns
    hist = hist.sort_index()
    return hist


def _incremental_sync(hist):
    """Try to update each source with latest values. Never raises; returns hist
    plus a dict of per-source success/failure."""
    status = {}
    end = datetime.now().strftime("%Y%m%d")
    # pull ~30d back for each source to catch late arrivals
    start_30 = (datetime.now() - timedelta(days=35)).strftime("%Y%m%d")

    def upsert_series(hist, colname, new_series):
        if new_series is None or new_series.empty:
            return hist
        if colname not in hist.columns:
            hist[colname] = np.nan
        for dt, val in new_series.items():
            hist.at[dt, colname] = val
        return hist

    def try_pull(name, fn):
        try:
            v = fn()
            status[name] = "ok"
            _state["consecutive_fails"][name] = 0
            return v
        except Exception as e:
            status[name] = f"error: {type(e).__name__}: {str(e)[:80]}"
            _state["consecutive_fails"][name] = _state["consecutive_fails"].get(name, 0) + 1
            return None

    us3y = try_pull("us3y",   lambda: _fetch_fred_dgs3(start_30, end))
    vix  = try_pull("vix",    lambda: _fetch_yf("^VIX", "vix", period="1mo"))
    try:
        usdkrw = try_pull("usdkrw", lambda: _fetch_yf("USDKRW=X", "usdkrw", period="1mo"))
    except Exception:
        usdkrw = None
    if usdkrw is None or (isinstance(usdkrw, pd.Series) and usdkrw.empty):
        usdkrw = try_pull("usdkrw_fallback", _fetch_usdkrw_fallback)
    sk = try_pull("sk", lambda: _fetch_yf("000660.KS", "sk", period="1mo"))
    ss = try_pull("ss", lambda: _fetch_yf("005930.KS", "ss", period="1mo"))
    fo_pair_sk = try_pull("fo_000660", lambda: _fetch_naver_foreign("000660", start_30, end))
    fo_pair_ss = try_pull("fo_005930", lambda: _fetch_naver_foreign("005930", start_30, end))

    hist = upsert_series(hist, "us3y",   us3y)
    hist = upsert_series(hist, "vix",    vix)
    hist = upsert_series(hist, "usdkrw", usdkrw)
    hist = upsert_series(hist, "sk",     sk)
    hist = upsert_series(hist, "ss",     ss)
    if fo_pair_sk:
        fo, cl = fo_pair_sk
        hist = upsert_series(hist, "fo_sk", fo)
        hist = upsert_series(hist, "cl_sk", cl)
    if fo_pair_ss:
        fo, cl = fo_pair_ss
        hist = upsert_series(hist, "fo_ss", fo)
        hist = upsert_series(hist, "cl_ss", cl)
    return hist.sort_index(), status


# ═════════════════════════════════════════════════════════════════
# COMPONENT COMPUTATION (mirrors Phase 0 percentile 120d)
# ═════════════════════════════════════════════════════════════════

def _rolling_percentile(series, window=ROLLING_WINDOW):
    def rank_last(x):
        v = x.iloc[-1]
        if pd.isna(v):
            return np.nan
        arr = x.dropna().values
        if len(arr) < max(20, int(window * 0.4)):
            return np.nan
        return round((arr < v).mean() * 100, 2)
    return series.rolling(window, min_periods=max(20, int(window * 0.4))).apply(rank_last, raw=False)


def _compute_component_series(hist):
    """Return dict of component score series (0-100) aligned to hist index."""
    # Align to Samsung trading days as canonical (KOSPI open days)
    idx = hist["ss"].dropna().index
    if len(idx) == 0:
        return None

    us3y_d   = hist["us3y"].reindex(idx).ffill()
    vix_d    = hist["vix"].reindex(idx).ffill()
    usdkrw_d = hist["usdkrw"].reindex(idx).ffill()
    sk_d     = hist["sk"].reindex(idx).ffill()
    ss_d     = hist["ss"].reindex(idx).ffill()
    fo_sk_d  = hist["fo_sk"].reindex(idx).ffill()
    fo_ss_d  = hist["fo_ss"].reindex(idx).ffill()
    cl_sk_d  = hist["cl_sk"].reindex(idx).ffill()
    cl_ss_d  = hist["cl_ss"].reindex(idx).ffill()

    # mcap-weighted foreign ownership
    mcap_sk = cl_sk_d * SK_SHARES
    mcap_ss = cl_ss_d * SS_SHARES
    fo_wa = (fo_sk_d * mcap_sk + fo_ss_d * mcap_ss) / (mcap_sk + mcap_ss)

    # C1: US 3Y (placeholder-3.5 offset cancels in percentile; keep for symmetry)
    us_rate_stress = _rolling_percentile(us3y_d - 3.5)

    # C2: VIX
    risk_sentiment = _rolling_percentile(vix_d)

    # C3: -Δ5 fo_wa (drop = stress)
    foreign_flow = _rolling_percentile(-fo_wa.diff(5))

    # C4: USDKRW pct_change(5)*100 + rolling std(20)*100
    c4_raw = usdkrw_d.pct_change(5) * 100 + usdkrw_d.pct_change().rolling(20).std() * 100
    fx_momentum = _rolling_percentile(c4_raw)

    # C5: mcap-weighted 5d return, inverted
    sk_r5 = sk_d.pct_change(5)
    ss_r5 = ss_d.pct_change(5)
    wa_ret = (sk_r5 * SK_SHARES * sk_d + ss_r5 * SS_SHARES * ss_d) / \
             (SK_SHARES * sk_d + SS_SHARES * ss_d)
    semiconductor = _rolling_percentile(-wa_ret * 100)

    return {
        "us_rate_stress":   us_rate_stress,
        "risk_sentiment":   risk_sentiment,
        "foreign_flow":     foreign_flow,
        "fx_momentum":      fx_momentum,
        "semiconductor":    semiconductor,
        # raw side-channels for API response
        "_raw": {
            "us_rate_stress":  us3y_d,
            "risk_sentiment":  vix_d,
            "foreign_flow":    fo_wa,
            "fx_momentum":     usdkrw_d,
            "semiconductor":   sk_d,  # SK Hynix as headline
        },
        "_semi_pair_last": {"sk": float(sk_d.iloc[-1]), "ss": float(ss_d.iloc[-1])},
    }


def _generate_ai_note(result):
    """Generate one-sentence factual summary. NEVER trading advice.
    graceful failure returns None (caller keeps ai_note=None)."""
    if _AI_CLIENT is None:
        return None
    try:
        score = result.get("score")
        regime = result.get("regime")
        direction = result.get("direction")
        comps = result.get("components", {})
        cs = {k: (v.get("score") if v else None) for k, v in comps.items()}
        prompt = (
            "You produce ONE factual English sentence (<=35 words) summarizing a KRW macro-stress score.\n"
            "STRICT RULES:\n"
            "- State the numeric score, regime, and which components are elevated or muted.\n"
            "- NO trading advice, NO recommendations, NO 'consider'/'suggest'/'buy'/'sell'/'position'/'hedge'.\n"
            "- NO forecasts (do not predict future price/rate direction).\n"
            "- Just facts about the current reading.\n\n"
            f"Score: {score} ({regime}), direction: {direction}\n"
            f"Component scores (0-100): {cs}\n\n"
            "Write ONE sentence, factual only."
        )
        resp = _AI_CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        # forbidden token filter — if AI slipped, redact
        forbidden = ["buy", "sell", "consider", "recommend", "suggest",
                     "position", "hedge", "should", "will likely", "forecast",
                     "prediction", "expect price"]
        low = text.lower()
        if any(f in low for f in forbidden):
            return None
        return text or None
    except Exception as e:
        print(f"[KRW-MACRO] ai_note generation skipped: {type(e).__name__}: {str(e)[:80]}")
        return None


def _regime(score):
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return "unknown"
    if score < 20: return "calm"
    if score < 40: return "neutral"
    if score < 60: return "caution"
    if score < 80: return "risk_off"
    return "crisis"


def _direction(usdkrw_series):
    tail = usdkrw_series.dropna().tail(6)
    if len(tail) < 6:
        return "unknown"
    now = tail.iloc[-1]; past = tail.iloc[0]
    if past == 0: return "unknown"
    chg = (now - past) / past
    if chg >= 0.003:  return "krw_weakening"
    if chg <= -0.003: return "krw_strengthening"
    return "stable"


def _market_hours():
    """Return dict {krx, us} based on UTC now."""
    now = datetime.now(timezone.utc)
    # KRX: 09:00-15:30 KST (UTC 00:00-06:30), Mon-Fri
    kst_hour = (now.hour * 60 + now.minute + 9 * 60) % (24 * 60)
    krx_open = (now.weekday() < 5) and (0 <= (now.hour * 60 + now.minute) < 6*60 + 30)
    # US regular: 09:30-16:00 EST/EDT (UTC 13:30-20:00 or 14:30-21:00). Use 13:30-21:00 window.
    us_min = now.hour * 60 + now.minute
    us_open = (now.weekday() < 5) and (13*60 + 30 <= us_min < 21*60)
    return {
        "krx": "open" if krx_open else "closed",
        "us":  "open" if us_open  else "closed",
    }


# ═════════════════════════════════════════════════════════════════
# COMPUTE FINAL API RESPONSE
# ═════════════════════════════════════════════════════════════════

def _compute_result(hist, incremental_status=None):
    comps = _compute_component_series(hist)
    if comps is None:
        raise RuntimeError("no component data")

    # latest scores per component
    latest = {}
    degraded = []
    now = datetime.now(timezone.utc)

    component_response = {}
    raw = comps["_raw"]
    fail_counts = _state["consecutive_fails"]
    for key in ["us_rate_stress", "risk_sentiment", "foreign_flow", "fx_momentum", "semiconductor"]:
        s = comps[key].dropna()
        if s.empty:
            degraded.append(key)
            component_response[key] = {"score": None, "raw": None, "freshness": "unavailable"}
            continue
        latest[key] = float(s.iloc[-1])
        # raw for API
        raw_series = raw[key].dropna()
        raw_val = float(raw_series.iloc[-1]) if not raw_series.empty else None
        raw_date = raw_series.index[-1] if not raw_series.empty else None
        freshness = None
        if raw_date is not None:
            age_days = (pd.Timestamp(now).tz_localize(None).normalize() - raw_date.normalize()).days
            freshness = f"{age_days}d"
        # foreign_flow raw special formatting
        if key == "foreign_flow":
            fo_wa_last = raw_val
            fo_wa_5d_ago = None
            if len(raw_series) > 5:
                fo_wa_5d_ago = float(raw_series.iloc[-6])
            delta = (fo_wa_last - fo_wa_5d_ago) if (fo_wa_last is not None and fo_wa_5d_ago is not None) else None
            raw_payload = {
                "mcap_weighted_foreign_pct": round(fo_wa_last, 3) if fo_wa_last else None,
                "delta_5d_pp":               round(delta, 3) if delta is not None else None,
                "note":                      "(proxy: SK Hynix + Samsung mcap-weighted foreign ownership %; not direct netbuy amount)",
            }
        elif key == "us_rate_stress":
            raw_payload = {"us_3y_yield_pct": round(raw_val, 3) if raw_val is not None else None}
        elif key == "risk_sentiment":
            raw_payload = {"vix": round(raw_val, 2) if raw_val is not None else None}
        elif key == "fx_momentum":
            r5 = None
            if len(raw_series) > 5:
                r5 = (raw_series.iloc[-1] / raw_series.iloc[-6] - 1) * 100
            raw_payload = {"usdkrw": round(raw_val, 2) if raw_val is not None else None,
                           "pct_change_5d": round(r5, 3) if r5 is not None else None}
        elif key == "semiconductor":
            raw_payload = {"sk_hynix_krw": comps["_semi_pair_last"]["sk"],
                           "samsung_krw":  comps["_semi_pair_last"]["ss"]}
        else:
            raw_payload = {"value": raw_val}

        if fail_counts.get(key, 0) >= STRUCTURAL_FAIL_THRESHOLD or (
            raw_date is not None and (pd.Timestamp(now).tz_localize(None).normalize() - raw_date.normalize()).days > 10
        ):
            degraded.append(key)

        component_response[key] = {
            "score": round(latest[key], 2),
            "raw": raw_payload,
            "freshness": freshness or "unknown",
        }

    # Weighted score with renormalization if any degraded
    active_weights = {k: v for k, v in WEIGHTS.items() if k not in degraded and latest.get(k) is not None}
    if not active_weights:
        raise RuntimeError("no active components")
    total_w = sum(active_weights.values())
    weights_normed = {k: w / total_w for k, w in active_weights.items()}
    final_score = sum(latest[k] * weights_normed[k] for k in weights_normed)

    fx_series = comps["_raw"]["fx_momentum"]
    direction = _direction(fx_series)

    result = {
        "score":     round(final_score, 2),
        "regime":    _regime(final_score),
        "direction": direction,
        "components": component_response,
        "ai_note":   None,  # populated on cache creation only
        "market_hours": _market_hours(),
        "as_of":     now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method":    f"rolling percentile {ROLLING_WINDOW}d, weights " +
                     "/".join(str(int(w * 100)) for w in WEIGHTS.values()),
        "degraded":  degraded,
        "_meta": {
            "sources_status": incremental_status or {},
            "history_rows":   len(hist),
            "score_series_last_date":
                comps["us_rate_stress"].dropna().index[-1].strftime("%Y-%m-%d") if not comps["us_rate_stress"].dropna().empty else None,
        },
    }
    return result


# ═════════════════════════════════════════════════════════════════
# SNAPSHOT PERSISTENCE
# ═════════════════════════════════════════════════════════════════

def _save_snapshot(hist, result):
    try:
        hist_records = []
        for dt, row in hist.iterrows():
            rec = {"d": dt.strftime("%Y-%m-%d")}
            for c in ("us3y", "vix", "usdkrw", "sk", "ss", "fo_sk", "fo_ss", "cl_sk", "cl_ss"):
                v = row.get(c)
                rec[c] = None if pd.isna(v) else float(v)
            hist_records.append(rec)
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump({"hist": hist_records, "result": result, "saved_at": time.time()}, f)
        return True
    except Exception as e:
        print(f"[KRW-MACRO] snapshot save failed: {e}")
        return False


def _load_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return None, None
    try:
        with open(SNAPSHOT_FILE) as f:
            d = json.load(f)
        recs = d.get("hist", [])
        if not recs:
            return None, None
        df = pd.DataFrame(recs)
        df["d"] = pd.to_datetime(df["d"])
        df = df.set_index("d").sort_index()
        return df, d.get("result")
    except Exception as e:
        print(f"[KRW-MACRO] snapshot load failed: {e}")
        return None, None


# ═════════════════════════════════════════════════════════════════
# PUBLIC API (async wrapper matches kr_news.py signature)
# ═════════════════════════════════════════════════════════════════

def _get_lock():
    if _state["lock"] is None:
        _state["lock"] = asyncio.Lock()
    return _state["lock"]


async def fetch_krw_macro_stress():
    """Public API. Returns full response dict. Never raises for a cached fallback."""
    now = time.time()
    # cache hit
    if _state["result"] is not None and now < _state["expires_at"]:
        cached = dict(_state["result"])
        cached["_meta"] = dict(cached.get("_meta", {}))
        cached["_meta"]["cache_age_seconds"] = int(now - (cached["_meta"].get("_computed_ts", now)))
        return cached

    async with _get_lock():
        if _state["result"] is not None and time.time() < _state["expires_at"]:
            cached = dict(_state["result"])
            cached["_meta"] = dict(cached.get("_meta", {}))
            cached["_meta"]["cache_age_seconds"] = int(time.time() - (cached["_meta"].get("_computed_ts", time.time())))
            return cached

        # ensure history exists
        if _state["history"] is None or _state["history"].empty:
            hist, saved_result = _load_snapshot()
            if hist is None or hist.empty:
                try:
                    hist = await asyncio.get_event_loop().run_in_executor(None, _full_backfill)
                    _state["last_backfill_ok"] = time.time()
                except Exception as e:
                    # Boot-time backfill failed and no snapshot → error surfaces to caller
                    raise RuntimeError(f"backfill failed and no snapshot: {e}")
            _state["history"] = hist

        # incremental sync
        try:
            new_hist, sync_status = await asyncio.get_event_loop().run_in_executor(
                None, _incremental_sync, _state["history"])
            _state["history"] = new_hist
        except Exception as e:
            sync_status = {"error": str(e)}

        result = _compute_result(_state["history"], sync_status)
        result["_meta"]["_computed_ts"] = time.time()
        result["_meta"]["cache_age_seconds"] = 0

        # ai_note only on cache creation (Haiku 1 call, graceful failure)
        try:
            note = await asyncio.get_event_loop().run_in_executor(None, _generate_ai_note, result)
            result["ai_note"] = note
        except Exception:
            result["ai_note"] = None

        _state["result"] = result
        _state["expires_at"] = time.time() + CACHE_TTL
        _save_snapshot(_state["history"], result)

        return result


# ═════════════════════════════════════════════════════════════════
# BOOT — load snapshot if present, else full backfill (once)
# ═════════════════════════════════════════════════════════════════

def _boot_init():
    hist, saved_result = _load_snapshot()
    if hist is not None and not hist.empty:
        _state["history"] = hist
        if saved_result is not None:
            _state["result"] = saved_result
            _state["expires_at"] = time.time()  # forces first request to recompute
        print(f"[KRW-MACRO] snapshot loaded: {len(hist)} rows, "
              f"{hist.index[0].date()} → {hist.index[-1].date()}")
        return
    # No snapshot — full backfill inline (module import blocks until done, mirroring kr_sentiment pattern)
    try:
        t0 = time.time()
        hist = _full_backfill()
        _state["history"] = hist
        _state["last_backfill_ok"] = time.time()
        # Save immediately so restarts don't re-fetch
        _save_snapshot(hist, None)
        print(f"[KRW-MACRO] full backfill done in {time.time()-t0:.1f}s: {len(hist)} rows, "
              f"{hist.index[0].date()} → {hist.index[-1].date()}")
    except Exception as e:
        print(f"[KRW-MACRO] boot backfill failed: {e}")


_boot_init()
print("[KRW-MACRO] krw_macro module loaded")
