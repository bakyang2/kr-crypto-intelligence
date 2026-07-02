"""
kr_news.py — Korean news → English API (K-pop and semiconductor categories).

Pipeline:
  1. Pick rotating Korean query keywords (broad-term heavy + a few top entities).
  2. Naver News API parallel fetch + dedupe by link.
  3. Claude Haiku 4.5 batch: classify is_relevant + translate title/summary +
     surface new entities not in the seed list.
  4. Optional premium tier: Claude Sonnet 4.6 synthesis (sentiment, themes,
     trending entities, market signal for semi).
  5. 5-min in-memory cache per (category, premium, limit).

Cost target: Haiku ≈ $0.001-0.005/call, Sonnet (premium) ≈ $0.015-0.025/call.
Naver quota: ~10 keyword calls per cold request → 5min cache → safe under 25k/day.
"""

import os
import re
import json
import time
import html
import random
import asyncio
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
import anthropic

from stats_logger import log_event


# === Config ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

NAVER_URL = "https://openapi.naver.com/v1/search/news.json"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

CACHE_TTL = 300  # 5 min
KST = timezone(timedelta(hours=9))

# Module-level Anthropic client — thread-safe per Anthropic SDK contract.
# Sync messages.create() invoked through loop.run_in_executor.
_ANTHROPIC = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=30.0)


# === Cache ===
_cache = {}              # cache_key -> (data, expires_at)
_locks = {}              # cache_key -> asyncio.Lock (per-key, avoids thundering herd)


# === Operational alerts (fallback / count-short / synthesis-error) ===
# 5-min dedupe per (alert_type, endpoint) to keep Telegram noise low.
_alert_dedupe = {}       # f"{alert_type}:{endpoint}" -> last-send unix ts
_ALERT_DEDUPE_TTL = 300  # 5 minutes


def _now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _build_alert_msg(alert_type: str, endpoint: str, details: dict) -> str:
    """Render alert message for Telegram (HTML mode). Plain enough to read in chat."""
    ts = _now_kst_str()
    if alert_type == "fallback":
        return (
            f"⚠️ <b>kr-news fallback 발동</b>\n"
            f"엔드포인트: {endpoint}\n"
            f"limit: {details.get('limit')}\n"
            f"1차 candidates: {details.get('pool_initial_count')}\n"
            f"2차 candidates: {details.get('pool_extra_count')}\n"
            f"1차 통과: {details.get('stage1_relevant_count')}건 (limit 부족)\n"
            f"최종 count: {details.get('final_count')}건\n"
            f"시간: {ts}"
        )
    if alert_type == "count_below_limit":
        return (
            f"🚨 <b>kr-news count 부족</b>\n"
            f"엔드포인트: {endpoint}\n"
            f"requested limit: {details.get('limit')}\n"
            f"returned count: {details.get('count')}\n"
            f"fallback used: {details.get('fallback_used')}\n"
            f"candidates 총 처리: {details.get('candidates_processed')}\n"
            f"원인 추정:\n"
            f"  - is_relevant 통과 비율 낮음 (Naver 결과 노이즈 多)\n"
            f"  - 키워드 사전 확장 필요할 수 있음\n"
            f"시간: {ts}"
        )
    if alert_type == "synthesis_error":
        return (
            f"❌ <b>kr-news AI synthesis 실패</b>\n"
            f"엔드포인트: {endpoint}\n"
            f"함수: {details.get('function')}\n"
            f"모델: {details.get('model')}\n"
            f"에러 유형: {details.get('error_type')}\n"
            f"에러 메시지: {(details.get('error_message') or '')[:200]}\n"
            f"영향: {details.get('impact')}\n"
            f"시간: {ts}"
        )
    return f"[kr-news] {alert_type} on {endpoint} — {details}"


async def _send_krnews_alert(alert_type: str, endpoint: str, details: dict):
    """Telegram alert for kr-news operational events.
    - 5-min dedupe by (alert_type, endpoint)
    - Env flag ENABLE_KR_NEWS_ALERTS (default 'true') to disable
    - Fire-and-forget: tg_send invoked via create_task; never blocks caller

    Intended to be called as `asyncio.create_task(_send_krnews_alert(...))`
    so even the dedupe check + message build happen off the request path."""
    if os.getenv("ENABLE_KR_NEWS_ALERTS", "true").lower() != "true":
        return
    now = time.time()
    # cleanup expired dedupe entries
    expired = [k for k, ts in _alert_dedupe.items() if now - ts > _ALERT_DEDUPE_TTL]
    for k in expired:
        del _alert_dedupe[k]
    dedupe_key = f"{alert_type}:{endpoint}"
    if dedupe_key in _alert_dedupe:
        return  # within 5-min window, already alerted
    _alert_dedupe[dedupe_key] = now

    msg = _build_alert_msg(alert_type, endpoint, details)

    # Lazy import to avoid circular dependency at module-load time.
    # main.py imports kr_news, so importing main here works only at call time.
    try:
        from main import tg_send
        asyncio.create_task(tg_send(msg))
    except Exception as e:
        print(f"[KR-NEWS-ALERT] tg_send dispatch failed: {e}")


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


# === Keyword sets ===
_keywords = {}           # category -> raw dict from JSON
_new_entities = {}       # category -> set of unseen entity names


def _load_keywords(category: str) -> dict:
    if category in _keywords:
        return _keywords[category]
    path = os.path.join(DATA_DIR, f"{category}_keywords.json")
    with open(path) as f:
        d = json.load(f)
    _keywords[category] = d
    return d


def _new_entities_path(category: str) -> str:
    return os.path.join(DATA_DIR, f"{category}_new_entities.json")


def _load_new_entities(category: str) -> set:
    if category in _new_entities:
        return _new_entities[category]
    path = _new_entities_path(category)
    s = set()
    if os.path.exists(path):
        try:
            with open(path) as f:
                s = set(json.load(f).get("entities", []))
        except Exception:
            pass
    _new_entities[category] = s
    return s


def _save_new_entities(category: str):
    s = _new_entities.get(category)
    if s is None:
        return
    path = _new_entities_path(category)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {"entities": sorted(s), "updated_at": int(time.time())},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, path)
    except Exception as e:
        print(f"[KR-NEWS] save new_entities failed: {e}")


def _flat_keywords(category: str) -> list:
    """All keywords from all tiers as flat [{kr, en, tier}, ...]."""
    d = _load_keywords(category)
    out = []
    for tier_name, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            for entry in v:
                kr = entry.get("kr", "")
                en = entry.get("en", "")
                if kr or en:
                    out.append({"kr": kr, "en": en, "tier": tier_name})
    return out


def _select_query_keywords(category: str, n_broad: int = 3, n_top: int = 7) -> list:
    """Pick rotating Korean query terms.
    Heavily tilted toward Tier 1/2 entities (BTS, Samsung Electronics, etc.) since
    those guarantee high relevance hit rate. A few Tier 5 broad terms catch breaking news.
    Returns list of Korean strings to query Naver with."""
    d = _load_keywords(category)
    broad = d.get("tier5_broad_terms", [])
    top1 = d.get("tier1_global_top") or d.get("tier1_companies", []) or []
    top2 = d.get("tier2_popular") or d.get("tier2_technologies", []) or []

    picks = []
    if broad:
        picks += random.sample(broad, min(n_broad, len(broad)))
    pool = list(top1) + list(top2)
    if pool:
        picks += random.sample(pool, min(n_top, len(pool)))

    # Naver indexes Korean text best — query in Korean
    return [p["kr"] for p in picks if isinstance(p, dict) and p.get("kr")]


# Semaphore to cap concurrent Naver calls (Naver enforces ~10 req/sec; stay safe at 4)
_NAVER_SEMAPHORE = asyncio.Semaphore(4)


# === HTML / pubDate utilities ===
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _parse_pubdate(s: str):
    """RFC 2822 (Naver) → ISO 8601 KST string. Returns None on failure."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).isoformat()
    except Exception:
        return None


# =============================================================================
# Korean media outlet hostname → {Korean name, English name} mapping.
# Subdomain-specific entries (e.g., biz.chosun.com) take priority over root
# domain (chosun.com). Standard romanization / canonical English names used.
# =============================================================================
SOURCE_KR_TO_EN = {
    # === Major dailies ===
    "chosun.com": {"kr": "조선일보", "en": "Chosun Ilbo"},
    "biz.chosun.com": {"kr": "조선비즈", "en": "Chosun Biz"},
    "it.chosun.com": {"kr": "IT조선", "en": "IT Chosun"},
    "donga.com": {"kr": "동아일보", "en": "Dong-A Ilbo"},
    "it.donga.com": {"kr": "IT동아", "en": "IT Donga"},
    "sports.donga.com": {"kr": "스포츠동아", "en": "Sports Donga"},
    "joongang.co.kr": {"kr": "중앙일보", "en": "JoongAng Ilbo"},
    "hani.co.kr": {"kr": "한겨레", "en": "The Hankyoreh"},
    "khan.co.kr": {"kr": "경향신문", "en": "Kyunghyang Shinmun"},
    "sports.khan.co.kr": {"kr": "스포츠경향", "en": "Sports Kyunghyang"},
    "kmib.co.kr": {"kr": "국민일보", "en": "Kookmin Ilbo"},
    "segye.com": {"kr": "세계일보", "en": "Segye Ilbo"},
    "munhwa.com": {"kr": "문화일보", "en": "Munhwa Ilbo"},
    "seoul.co.kr": {"kr": "서울신문", "en": "Seoul Shinmun"},
    "hankookilbo.com": {"kr": "한국일보", "en": "Hankook Ilbo"},

    # === Economic / business ===
    "hankyung.com": {"kr": "한국경제", "en": "Korea Economic Daily"},
    "tenasia.hankyung.com": {"kr": "텐아시아", "en": "TenAsia"},
    "mk.co.kr": {"kr": "매일경제", "en": "Maeil Business Newspaper"},
    "mt.co.kr": {"kr": "머니투데이", "en": "Money Today"},
    "moneys.mt.co.kr": {"kr": "머니S", "en": "MoneyS"},
    "moneys.co.kr": {"kr": "머니S", "en": "MoneyS"},
    "edaily.co.kr": {"kr": "이데일리", "en": "EDaily"},
    "sedaily.com": {"kr": "서울경제", "en": "The Seoul Economic Daily"},
    "fnnews.com": {"kr": "파이낸셜뉴스", "en": "Financial News"},
    "asiae.co.kr": {"kr": "아시아경제", "en": "Asia Business Daily"},
    "heraldcorp.com": {"kr": "헤럴드경제", "en": "Herald Business"},
    "biz.heraldcorp.com": {"kr": "헤럴드경제", "en": "Herald Business"},
    "heraldpop.com": {"kr": "헤럴드POP", "en": "Herald Pop"},
    "businesspost.co.kr": {"kr": "비즈니스포스트", "en": "Business Post"},
    "bizwatch.co.kr": {"kr": "비즈워치", "en": "Business Watch"},
    "thebell.co.kr": {"kr": "더벨", "en": "The Bell"},
    "investchosun.com": {"kr": "인베스트조선", "en": "Invest Chosun"},

    # === IT / electronics / tech ===
    "etnews.com": {"kr": "전자신문", "en": "Electronic Times News"},
    "dt.co.kr": {"kr": "디지털타임스", "en": "Digital Times"},
    "ddaily.co.kr": {"kr": "디지털데일리", "en": "Digital Daily"},
    "zdnet.co.kr": {"kr": "ZDNet Korea", "en": "ZDNet Korea"},
    "bloter.net": {"kr": "블로터", "en": "Bloter"},
    "aitimes.com": {"kr": "AI타임스", "en": "AI Times"},
    "aitimes.kr": {"kr": "AI타임스", "en": "AI Times"},

    # === Wire services / news agencies ===
    "yna.co.kr": {"kr": "연합뉴스", "en": "Yonhap News"},
    "yonhapnewstv.co.kr": {"kr": "연합뉴스TV", "en": "Yonhap News TV"},
    "newsis.com": {"kr": "뉴시스", "en": "Newsis"},
    "news1.kr": {"kr": "뉴스1", "en": "News1"},

    # === Broadcasters ===
    "kbs.co.kr": {"kr": "KBS", "en": "KBS"},
    "news.kbs.co.kr": {"kr": "KBS", "en": "KBS"},
    "mbc.co.kr": {"kr": "MBC", "en": "MBC"},
    "imbc.com": {"kr": "MBC", "en": "MBC"},
    "imnews.imbc.com": {"kr": "MBC", "en": "MBC"},
    "sbs.co.kr": {"kr": "SBS", "en": "SBS"},
    "news.sbs.co.kr": {"kr": "SBS", "en": "SBS"},
    "biz.sbs.co.kr": {"kr": "SBS Biz", "en": "SBS Biz"},
    "jtbc.co.kr": {"kr": "JTBC", "en": "JTBC"},
    "news.jtbc.co.kr": {"kr": "JTBC", "en": "JTBC"},
    "ytn.co.kr": {"kr": "YTN", "en": "YTN"},
    "ichannela.com": {"kr": "채널A", "en": "Channel A"},
    "tvchosun.com": {"kr": "TV조선", "en": "TV Chosun"},
    "news.tvchosun.com": {"kr": "TV조선", "en": "TV Chosun"},
    "mbn.co.kr": {"kr": "MBN", "en": "Maeil Broadcasting Network"},
    "mbn.mk.co.kr": {"kr": "MBN", "en": "Maeil Broadcasting Network"},

    # === Sports / entertainment ===
    "sports.chosun.com": {"kr": "스포츠조선", "en": "Sports Chosun"},
    "sportsseoul.com": {"kr": "스포츠서울", "en": "Sports Seoul"},
    "osen.co.kr": {"kr": "OSEN", "en": "OSEN"},
    "isplus.com": {"kr": "일간스포츠", "en": "Ilgan Sports"},
    "starnewskorea.com": {"kr": "스타뉴스", "en": "Star News"},
    "mydaily.co.kr": {"kr": "마이데일리", "en": "MyDaily"},
    "tenasia.com": {"kr": "텐아시아", "en": "TenAsia"},
    "tenasia.co.kr": {"kr": "텐아시아", "en": "TenAsia"},
    "topstarnews.net": {"kr": "톱스타뉴스", "en": "TopStarNews"},
    "newsen.com": {"kr": "뉴스엔", "en": "Newsen"},
    "spotvnews.co.kr": {"kr": "스포티비뉴스", "en": "SPOTV News"},

    # === Online / opinion ===
    "ohmynews.com": {"kr": "오마이뉴스", "en": "OhmyNews"},
    "pressian.com": {"kr": "프레시안", "en": "Pressian"},
    "mediatoday.co.kr": {"kr": "미디어오늘", "en": "Media Today"},
    "nocutnews.co.kr": {"kr": "노컷뉴스", "en": "NoCut News"},
    "dailian.co.kr": {"kr": "데일리안", "en": "Dailian"},
    "tf.co.kr": {"kr": "더팩트", "en": "The Fact"},
    "ilyo.co.kr": {"kr": "일요신문", "en": "Ilyo Shinmun"},
    "sisajournal.com": {"kr": "시사저널", "en": "Sisa Journal"},
    "sisain.co.kr": {"kr": "시사인", "en": "Sisa IN"},
    "weekly.donga.com": {"kr": "주간동아", "en": "Weekly Dong-A"},
    "weekly.chosun.com": {"kr": "주간조선", "en": "Weekly Chosun"},

    # === English-language Korean press ===
    "koreaherald.com": {"kr": "코리아헤럴드", "en": "The Korea Herald"},
    "koreatimes.co.kr": {"kr": "코리아타임스", "en": "The Korea Times"},
    "koreatimes.com": {"kr": "코리아타임스", "en": "The Korea Times"},
    "koreajoongangdaily.joins.com": {"kr": "코리아중앙데일리", "en": "Korea JoongAng Daily"},

    # === Regional ===
    "busan.com": {"kr": "부산일보", "en": "Busan Ilbo"},
    "imaeil.com": {"kr": "매일신문", "en": "Maeil Shinmun"},
    "yeongnam.com": {"kr": "영남일보", "en": "Yeongnam Ilbo"},
    "kwnews.co.kr": {"kr": "강원일보", "en": "Gangwon Ilbo"},
    "kwangju.co.kr": {"kr": "광주일보", "en": "Gwangju Ilbo"},
    "jejunews.com": {"kr": "제주일보", "en": "Jeju Ilbo"},
    "kbsm.net": {"kr": "경북신문", "en": "Gyeongbuk Shinmun"},
    "ulsanpress.net": {"kr": "울산매일", "en": "Ulsan Maeil"},
}


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _sanitize_source(s: str) -> str:
    """Defensive cleanup — strip Markdown link syntax, surrounding brackets/parens,
    URL scheme. Always returns plain alphanumeric host-like string.
    Guards against the case where upstream (rare) hands us a markdown-formatted
    string instead of a hostname."""
    if not s:
        return ""
    # Reduce '[text](url)' → 'text'
    m = _MARKDOWN_LINK_RE.search(s)
    if m:
        s = m.group(1)
    # Strip residual markdown / scheme / whitespace
    s = s.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
    s = s.replace("https://", "").replace("http://", "").strip()
    # Trim trailing slash + path if any leaked through
    if "/" in s:
        s = s.split("/", 1)[0]
    return s


def _resolve_source(host: str) -> dict:
    """Resolve URL host → {kr, en} via SOURCE_KR_TO_EN.
    Tries exact match first, then strips 'www.'/'m.', then walks subdomains
    to find longest-suffix match. Falls back to sanitized host for both names."""
    if not host:
        return {"kr": "", "en": ""}
    host_clean = _sanitize_source(host)
    if not host_clean:
        return {"kr": "", "en": ""}
    h = host_clean.lower()
    for prefix in ("www.", "m."):
        if h.startswith(prefix):
            h = h[len(prefix):]
            break
    if h in SOURCE_KR_TO_EN:
        return dict(SOURCE_KR_TO_EN[h])
    # walk subdomains: foo.bar.example.com → bar.example.com → example.com
    parts = h.split(".")
    while len(parts) > 2:
        parts = parts[1:]
        candidate = ".".join(parts)
        if candidate in SOURCE_KR_TO_EN:
            return dict(SOURCE_KR_TO_EN[candidate])
    # Fallback — return sanitized host, never raw markdown
    return {"kr": host_clean, "en": host_clean}


def _extract_source(link: str) -> dict:
    """Resolve a Naver article URL to {kr, en} source names."""
    try:
        host = urlparse(link).hostname or ""
        return _resolve_source(host)
    except Exception:
        return {"kr": "", "en": ""}


# === Naver News fetch ===
async def _fetch_naver(client: httpx.AsyncClient, query: str, display: int = 10) -> list:
    """Fetch with semaphore + 1 retry on 429 with backoff."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("[KR-NEWS] Naver creds missing")
        return []
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": display, "sort": "date"}
    async with _NAVER_SEMAPHORE:
        for attempt in range(2):
            try:
                r = await client.get(NAVER_URL, headers=headers, params=params, timeout=10.0)
                if r.status_code == 200:
                    return r.json().get("items", [])
                if r.status_code == 429 and attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                print(f"[KR-NEWS] Naver {r.status_code} for '{query}': {r.text[:120]}")
                return []
            except Exception as e:
                print(f"[KR-NEWS] Naver fetch error '{query}': {e}")
                return []
    return []


# === Claude classification + translation (Haiku) ===
def _classify_and_translate_sync(category: str, articles: list, seed_sample: list) -> dict:
    """Sync Claude Haiku call — invoked via loop.run_in_executor.
    Returns {"data": {"results": [...]}, "usage": {input_tokens, output_tokens}}."""

    article_text = ""
    for i, a in enumerate(articles, 1):
        title = a["title_kr"][:200]
        desc = a["description_kr"][:300]
        article_text += f"[{i}] title: {title}\n    description: {desc}\n\n"

    cat_label = (
        "Korean K-pop artists/groups (singers, idol bands, soloists, comebacks, music releases). "
        "Korean dramas, movies, sports, politics → false."
        if category == "kpop" else
        "Korean semiconductor industry — Samsung Electronics, SK Hynix, suppliers, equipment "
        "makers, memory products (HBM/DRAM/NAND), foundry, AI chips, semi tech, exports. "
        "Generic IT, smartphones (unless explicitly chip-related), other industries → false."
    )

    prompt = f"""You triage and translate Korean news articles for: {cat_label}

For each article, output:
1. is_relevant: true ONLY if the article is genuinely about the category above
2. title_en: clean English translation, drop media outlet brackets like [단독]/[속보]
3. summary_en: 1-2 sentence English summary (factual, no editorialization)
4. new_entities: list any group/artist/company names mentioned that are NOT in the seed list below; max 3 per article; empty list if none

Seed entities (partial): {", ".join(seed_sample[:60])}

Articles:
{article_text}

Output JSON only (no markdown, no preamble, no trailing text):
{{"results":[{{"idx":1,"is_relevant":true,"title_en":"...","summary_en":"...","new_entities":[]}}]}}"""

    text = ""
    try:
        msg = _ANTHROPIC.messages.create(
            model=HAIKU_MODEL,
            max_tokens=8000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "stop_reason": getattr(msg, "stop_reason", None),
        }
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        # Truncation detection (caller fires synthesis_error alert)
        if usage.get("stop_reason") == "max_tokens" and not text.rstrip().endswith("}"):
            print(f"[KR-NEWS] Haiku truncated at max_tokens — graceful fallback")
            return {"data": {"results": []}, "usage": usage,
                    "error_type": "truncation",
                    "error_message": f"stop_reason=max_tokens, tail={text[-100:]!r}"}
        parsed = json.loads(text)
        return {"data": parsed, "usage": usage}
    except json.JSONDecodeError as e:
        print(f"[KR-NEWS] Haiku JSON parse failed: {e}; first 200 chars: {text[:200]}")
        return {"data": {"results": []}, "usage": {"input_tokens": 0, "output_tokens": 0},
                "error_type": "json_parse",
                "error_message": f"{e}; first 200: {text[:200]}"}
    except Exception as e:
        print(f"[KR-NEWS] Haiku error: {e}")
        return {"data": {"results": []}, "usage": {"input_tokens": 0, "output_tokens": 0},
                "error_type": "api_error",
                "error_message": str(e)[:300]}


# === Premium synthesis (Sonnet) ===
def _premium_synthesize_sync(category: str, articles: list) -> dict:
    """Premium synthesis via Haiku 4.5 (downgraded from Sonnet 4.6 after 2026-05-11
    A/B benchmark — Haiku 3s faster, 66% cheaper, equivalent output quality).
    Returns {data: dict|None, usage: ...}. On JSON parse failure or truncation,
    data=None — caller treats as graceful fallback (results returned w/o ai_analysis)."""
    article_text = ""
    for i, a in enumerate(articles, 1):
        article_text += f"[{i}] {a['title_en']} — {a['summary_en']}\n"

    if category == "semiconductor":
        json_template = (
            '{"overall_sentiment":"positive|neutral|negative",'
            '"key_themes":["theme1","theme2","theme3"],'
            '"trending_entities":["..."],'
            '"market_signal":"bullish|bearish|neutral",'
            '"summary_en":"4-6 sentence synthesis (~200 words)"}'
        )
        analyst_role = "Korean semiconductor industry analyst (Samsung/SK Hynix, HBM, foundry, AI chip)"
    else:
        json_template = (
            '{"overall_sentiment":"positive|neutral|negative",'
            '"key_themes":["theme1","theme2","theme3"],'
            '"trending_entities":["..."],'
            '"summary_en":"4-6 sentence synthesis (~200 words)"}'
        )
        analyst_role = "Korean K-pop industry analyst"

    # summary_en length explicit so Haiku doesn't write 1-2 sentences (still
    # acceptable for premium tier, ~200 words).
    prompt = f"""You are a {analyst_role}.

Today's top headlines (English-translated):
{article_text}

Produce a structured analysis. Be specific and reference actual entities/products.

Constraints:
- summary_en: 4-6 sentences (~200 words) — substantive paragraph
- key_themes: exactly 3 short phrases
- trending_entities: 4-6 items, plain names only (no markdown)
- TOTAL response under 1500 tokens; no preamble; no markdown.

Output JSON only:
{json_template}"""

    text = ""
    try:
        msg = _ANTHROPIC.messages.create(
            model=HAIKU_MODEL,   # was SONNET_MODEL (claude-sonnet-4-6); switched 2026-05-11
            max_tokens=8000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "stop_reason": getattr(msg, "stop_reason", None),
        }
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        # Defensive: if model truncates (stop_reason=max_tokens), JSON likely
        # lacks closing brace. Detect and surface graceful fallback.
        if usage.get("stop_reason") == "max_tokens" and not text.rstrip().endswith("}"):
            print(f"[KR-NEWS] premium truncated at max_tokens — graceful fallback")
            return {"data": None, "usage": usage,
                    "error_type": "truncation",
                    "error_message": f"stop_reason=max_tokens, tail={text[-100:]!r}"}
        parsed = json.loads(text)
        # Validate expected fields — alert if missing
        required = ["overall_sentiment", "key_themes", "trending_entities", "summary_en"]
        if category == "semiconductor":
            required.append("market_signal")
        missing = [k for k in required if k not in parsed]
        if missing:
            return {"data": parsed, "usage": usage,
                    "error_type": "missing_fields",
                    "error_message": f"missing keys: {missing}"}
        return {"data": parsed, "usage": usage}
    except json.JSONDecodeError as e:
        print(f"[KR-NEWS] premium JSON decode error: {e}; first 200: {text[:200]}")
        return {"data": None, "usage": {"input_tokens": 0, "output_tokens": 0, "stop_reason": "json_error"},
                "error_type": "json_parse",
                "error_message": f"{e}; first 200: {text[:200]}"}
    except Exception as e:
        print(f"[KR-NEWS] premium error: {e}; first 200: {text[:200]}")
        return {"data": None, "usage": {"input_tokens": 0, "output_tokens": 0},
                "error_type": "api_error",
                "error_message": str(e)[:300]}


# === Main entry ===
async def fetch_kr_news(category: str, premium: bool = False, limit: int = 5) -> dict:
    """Fetch Korean news in English for the given category.

    Args:
        category: 'kpop' or 'semiconductor'.
        premium: If True, add Sonnet-driven analysis (sentiment / themes / market signal).
        limit: Max articles in results (default 5).
    """
    if category not in ("kpop", "semiconductor"):
        return {"ok": False, "error": "category must be 'kpop' or 'semiconductor'"}

    cache_key = f"krnews:{category}:{premium}:{limit}"
    now = time.time()

    # Fast cache hit (no lock)
    if cache_key in _cache:
        data, exp = _cache[cache_key]
        if now < exp:
            data2 = dict(data)
            data2["_meta"] = dict(data2.get("_meta", {}))
            data2["_meta"]["cache_age_seconds"] = int(now - (exp - CACHE_TTL))
            return data2

    # Slow path with per-key lock
    lock = _get_lock(cache_key)
    async with lock:
        now = time.time()
        if cache_key in _cache:
            data, exp = _cache[cache_key]
            if now < exp:
                data2 = dict(data)
                data2["_meta"] = dict(data2.get("_meta", {}))
                data2["_meta"]["cache_age_seconds"] = int(now - (exp - CACHE_TTL))
                return data2

        return await _compute_kr_news(category, premium, limit, cache_key)


async def _compute_kr_news(category: str, premium: bool, limit: int, cache_key: str) -> dict:
    # 1. Pick query keywords
    queries = _select_query_keywords(category, n_broad=8, n_top=4)
    if not queries:
        return {"ok": False, "error": "no keywords available"}

    # 2. Parallel Naver fetch
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_naver(client, q, display=10) for q in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # 3. Merge + dedupe by canonical link
    merged = []
    seen_links = set()
    for items in results:
        if isinstance(items, Exception) or not items:
            continue
        for it in items:
            link = (it.get("originallink") or it.get("link") or "").strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            merged.append({
                "title_kr": _strip_html(it.get("title", "")),
                "description_kr": _strip_html(it.get("description", "")),
                "link": link,
                "pubDate": it.get("pubDate", ""),
                "_pub_iso": _parse_pubdate(it.get("pubDate", "")),
            })

    # 4. Sort newest first
    def _ts(a):
        iso = a.get("_pub_iso")
        if not iso:
            return 0
        try:
            return datetime.fromisoformat(iso).timestamp()
        except Exception:
            return 0

    merged.sort(key=_ts, reverse=True)

    if not merged:
        result = {
            "ok": False,
            "category": category,
            "results": [],
            "count": 0,
            "error": "no news found",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "_meta": {"queries_used": queries, "cache_age_seconds": 0},
        }
        _cache[cache_key] = (result, time.time() + 60)  # short cache on empty
        return result

    # 5. Two-stage classify+translate to reduce typical Haiku cost.
    #    Stage 1 batch: limit*3 (floor 12). If yield meets limit, done.
    #    Stage 2 fallback: extra slice up to limit*5 (floor 16) only when needed.
    flat_kw = _flat_keywords(category)
    seed_sample = [k["kr"] for k in flat_kw[:60]] + [k["en"] for k in flat_kw[:30]]
    new_set = _load_new_entities(category)
    seed_kr = {k["kr"].lower() for k in flat_kw if k["kr"]}
    seed_en = {k["en"].lower() for k in flat_kw if k["en"]}

    stage1_size = max(limit * 3, 12)
    stage2_cap = max(limit * 5, 16)
    pool_initial = merged[:stage1_size]

    loop = asyncio.get_event_loop()
    endpoint_label = f"/api/v1/kr-news/{category}{'-summary' if premium else ''}"

    async def _classify_log(batch: list, stage_label: str):
        """Run classify on a batch + log usage + alert on Claude errors.
        Returns full cr dict ({"data": ..., "usage": ..., optional error_*})."""
        cr = await loop.run_in_executor(
            None, _classify_and_translate_sync, category, batch, seed_sample
        )
        try:
            usage = cr["usage"]
            cost_h = round(
                usage["input_tokens"] * 1.0e-6 + usage["output_tokens"] * 5.0e-6, 6
            )
            log_event(
                "claude_call",
                endpoint=f"kr-news/{category}",
                model="haiku-4.5",
                stage=stage_label,
                cost_usd=cost_h,
                tokens_in=usage["input_tokens"],
                tokens_out=usage["output_tokens"],
            )
        except Exception:
            pass
        # synthesis_error alert (truncation / json_parse / api_error) — fire-and-forget
        if cr.get("error_type"):
            asyncio.create_task(_send_krnews_alert(
                "synthesis_error",
                endpoint_label,
                {
                    "function": f"_classify_and_translate_sync ({stage_label})",
                    "model": "haiku-4.5",
                    "error_type": cr["error_type"],
                    "error_message": cr.get("error_message", ""),
                    "impact": "graceful fallback — empty results for this stage",
                },
            ))
        return cr

    def _consume(batch: list, classify_data: dict) -> list:
        """Match classifier output to candidates → relevant records.
        idx in classify_data is 1-based within the batch."""
        cmap = {r.get("idx"): r for r in (classify_data.get("results") or []) if isinstance(r, dict)}
        out = []
        for i, cand in enumerate(batch, 1):
            cls = cmap.get(i)
            if not cls or not cls.get("is_relevant"):
                continue
            src = _extract_source(cand["link"])
            out.append({
                "title_kr": cand["title_kr"],
                "title_en": (cls.get("title_en") or "").strip(),
                "summary_en": (cls.get("summary_en") or "").strip(),
                "source_kr": src["kr"],
                "source_en": src["en"],
                "published_at": cand.get("_pub_iso") or cand.get("pubDate"),
                "link": cand["link"],
            })
            for ent in (cls.get("new_entities") or []):
                if not isinstance(ent, str):
                    continue
                ent_norm = ent.strip()
                if not ent_norm or len(ent_norm) > 50:
                    continue
                low = ent_norm.lower()
                if low in seed_kr or low in seed_en:
                    continue
                new_set.add(ent_norm)
        return out

    # Stage 1
    classify_1 = await _classify_log(pool_initial, "primary")
    results_out = _consume(pool_initial, classify_1["data"])
    stage1_relevant_count = len(results_out)

    # Stage 2 — fallback only when yield insufficient AND we have more articles
    fallback_used = False
    candidates_processed = len(pool_initial)
    if len(results_out) < limit and len(merged) > stage1_size:
        pool_extra = merged[stage1_size:stage2_cap]
        if pool_extra:
            classify_2 = await _classify_log(pool_extra, "fallback")
            results_out.extend(_consume(pool_extra, classify_2["data"]))
            fallback_used = True
            candidates_processed = stage1_size + len(pool_extra)
            asyncio.create_task(_send_krnews_alert(
                "fallback",
                endpoint_label,
                {
                    "limit": limit,
                    "pool_initial_count": stage1_size,
                    "pool_extra_count": len(pool_extra),
                    "stage1_relevant_count": stage1_relevant_count,
                    "final_count": min(len(results_out), limit),
                },
            ))

    # Trim to requested limit + persist newly-discovered entities
    results_out = results_out[:limit]
    _save_new_entities(category)

    # Alert B: count below requested limit (after all stages + trim)
    if len(results_out) < limit:
        asyncio.create_task(_send_krnews_alert(
            "count_below_limit",
            endpoint_label,
            {
                "limit": limit,
                "count": len(results_out),
                "fallback_used": fallback_used,
                "candidates_processed": candidates_processed,
            },
        ))

    # 7. Premium synthesis (Haiku 4.5 — was Sonnet 4.6, switched 2026-05-11)
    ai_analysis = None
    if premium and results_out:
        synth = await loop.run_in_executor(
            None, _premium_synthesize_sync, category, results_out
        )
        ai_analysis = synth["data"]
        synth_usage = synth["usage"]
        try:
            cost_synth = round(
                synth_usage["input_tokens"] * 1.0e-6 + synth_usage["output_tokens"] * 5.0e-6, 6
            )
            log_event(
                "claude_call",
                endpoint=f"kr-news/{category}-summary",
                model="haiku-4.5",
                cost_usd=cost_synth,
                tokens_in=synth_usage["input_tokens"],
                tokens_out=synth_usage["output_tokens"],
            )
        except Exception:
            pass
        # Alert D: premium synthesis failure (truncation / json_parse / missing_fields)
        if synth.get("error_type"):
            impact = (
                "ai_analysis omitted from response"
                if synth.get("data") is None
                else "ai_analysis present but missing fields"
            )
            asyncio.create_task(_send_krnews_alert(
                "synthesis_error",
                endpoint_label,
                {
                    "function": "_premium_synthesize_sync",
                    "model": "haiku-4.5",
                    "error_type": synth["error_type"],
                    "error_message": synth.get("error_message", ""),
                    "impact": impact,
                },
            ))

    # 8. Build response
    result = {
        "ok": True,
        "category": category,
        "results": results_out,
        "count": len(results_out),
        "requested_limit": limit,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_meta": {
            "queries_used": queries,
            "candidates_processed": candidates_processed,
            "fallback_used": fallback_used,
            "cache_age_seconds": 0,
            "premium": premium,
            "new_entities_total": len(new_set),
        },
    }
    if premium:
        if ai_analysis is not None:
            result["ai_analysis"] = ai_analysis
        else:
            # Graceful: results returned, AI synthesis temporarily unavailable.
            # Client can retry or use just the headline list.
            result["ai_analysis"] = None
            result["_meta"]["ai_analysis_status"] = "synthesis_unavailable"

    _cache[cache_key] = (result, time.time() + CACHE_TTL)
    return result


print("[KR-NEWS] kr_news module loaded")
