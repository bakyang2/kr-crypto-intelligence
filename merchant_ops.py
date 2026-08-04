"""
merchant_ops.py — receipt issuance + post-settle monitoring.

Three subsystems:
1. Receipt signing — ECDSA secp256k1 receipt attached to every paid 200 response.
2. Post-settle failure detection — settle succeeded but handler 5xx → alert + log.
3. Telegram alert helper — fire-and-forget with 5-min dedupe + hourly cap.

Designed to be wired into main.py rate_limit_middleware after `call_next`.
"""
import os
import json
import time
import secrets
import asyncio
from datetime import datetime, timezone
from collections import defaultdict

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys import keys

from stats_logger import log_event


# === Receipt signer key management =========================================
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".receipt_signer_key")


def _load_or_generate_signer() -> Account:
    """Load ECDSA private key from env or .receipt_signer_key file.
    If neither exists, generate a new one and persist to file."""
    env_key = os.getenv("RECEIPT_SIGNER_PRIVATE_KEY", "").strip()
    if env_key:
        if not env_key.startswith("0x"):
            env_key = "0x" + env_key
        return Account.from_key(env_key)

    # File fallback
    if os.path.exists(_KEY_FILE):
        try:
            with open(_KEY_FILE, "r") as f:
                k = f.read().strip()
                if k:
                    if not k.startswith("0x"):
                        k = "0x" + k
                    return Account.from_key(k)
        except Exception as e:
            print(f"[RECEIPT] failed to read {_KEY_FILE}: {e}")

    # Generate + persist
    acct = Account.create()
    try:
        with open(_KEY_FILE, "w") as f:
            f.write(acct.key.hex())
        os.chmod(_KEY_FILE, 0o600)
        print(f"[RECEIPT] generated new signer key, persisted to {_KEY_FILE}")
        print(f"[RECEIPT] signer address: {acct.address}")
    except Exception as e:
        print(f"[RECEIPT] failed to persist signer key: {e} — will regenerate on restart")
    return acct


_SIGNER = _load_or_generate_signer()
SIGNER_ADDRESS = _SIGNER.address

# Uncompressed public key — 65 bytes (04 prefix + X + Y). Bots can verify ECDSA
# signatures against this. Stored as 0x-prefixed hex for the manifest.
try:
    _pubkey = keys.PrivateKey(_SIGNER.key).public_key
    SIGNER_PUBLIC_KEY = "0x04" + _pubkey.to_hex()[2:]  # 0x + 04 + 128 hex chars
except Exception as e:
    print(f"[RECEIPT] public key derive failed: {e}")
    SIGNER_PUBLIC_KEY = ""


# === Endpoint → price map (USD) ===========================================
# Single source of truth — mirrored from main.tg_notify_request price_map.
ENDPOINT_PRICES = {
    "/api/v1/kimchi-premium": "0.002",
    "/api/v1/kr-prices": "0.002",
    "/api/v1/fx-rate": "0.001",
    "/api/v1/stablecoin-premium": "0.002",
    "/api/v1/market-read": "0.10",
    "/api/v1/arbitrage-scanner": "0.01",
    "/api/v1/exchange-alerts": "0.01",
    "/api/v1/market-movers": "0.01",
    "/api/v1/kr-sentiment": "0.05",
    "/api/v1/global-vs-korea-divergence": "0.05",
    "/api/v1/global-vs-korea-divergence-deep": "0.10",
    "/api/v1/kr-news/kpop": "0.01",
    "/api/v1/kr-news/kpop-summary": "0.05",
    "/api/v1/kr-news/semiconductor": "0.02",
    "/api/v1/kr-news/semiconductor-summary": "0.10",
    "/api/v1/krw-macro-stress": "0.05",
    # XRPL variants — 1:1 price mirror. Currency is dispatched at receipt-
    # sign time (main.py caller passes currency="RLUSD"); only the numeric
    # amount is looked up here.
    "/api/v1/xrpl/kimchi-premium": "0.002",
    "/api/v1/xrpl/kr-prices": "0.002",
    "/api/v1/xrpl/fx-rate": "0.001",
    "/api/v1/xrpl/stablecoin-premium": "0.002",
    "/api/v1/xrpl/arbitrage-scanner": "0.01",
    "/api/v1/xrpl/exchange-alerts": "0.01",
    "/api/v1/xrpl/market-movers": "0.01",
    "/api/v1/xrpl/kr-news/kpop": "0.01",
    "/api/v1/xrpl/kr-news/semiconductor": "0.02",
    "/api/v1/xrpl/global-vs-korea-divergence": "0.05",
    "/api/v1/xrpl/kr-sentiment": "0.05",
    "/api/v1/xrpl/kr-news/kpop-summary": "0.05",
    "/api/v1/xrpl/global-vs-korea-divergence-deep": "0.10",
    "/api/v1/xrpl/market-read": "0.10",
    "/api/v1/xrpl/kr-news/semiconductor-summary": "0.10",
    "/api/v1/xrpl/krw-macro-stress": "0.05",
}


# === Receipt creation =======================================================
def _now_iso_utc() -> str:
    """RFC3339 UTC with milliseconds, ending in Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _today_yyyymmdd() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def create_receipt(endpoint: str, network: str, tx_hash: str, payer: str,
                   merchant: str, currency: str = "USDC") -> dict:
    """Build + sign a receipt. Raises if anything goes wrong; callers should
    catch and emit `_meta.receipt_status: generation_failed`.

    `currency` defaults to "USDC" (Base/Polygon/Solana). Callers on XRPL
    routes MUST pass currency="RLUSD" — dispatched per-endpoint in main.py
    alongside the merchant address dispatch (F5)."""
    issued_at = _now_iso_utc()
    rcpt_id = f"rcpt_{_today_yyyymmdd()}_{secrets.token_hex(3)}"
    amount = ENDPOINT_PRICES.get(endpoint, "0.001")
    network_s = network or "unknown"
    tx_s = tx_hash or ""
    payer_s = payer or ""

    payload_str = (
        f"{rcpt_id}|{endpoint}|{amount}|{currency}|{network_s}|{tx_s}|{payer_s}|{merchant}|{issued_at}"
    )
    msg = encode_defunct(text=payload_str)
    signed = _SIGNER.sign_message(msg)
    sig_hex = signed.signature.hex()
    if not sig_hex.startswith("0x"):
        sig_hex = "0x" + sig_hex

    return {
        "id": rcpt_id,
        "issued_at": issued_at,
        "endpoint": endpoint,
        "amount": amount,
        "currency": currency,
        "network": network_s,
        "tx_hash": tx_s,
        "payer": payer_s,
        "merchant": merchant,
        "signature": sig_hex,
        "signer": SIGNER_ADDRESS,
    }


# === Post-settle alert + dedupe ============================================
# 5-min dedupe per (endpoint, ip) + 1-hour rolling cap of 5 alerts per endpoint
# (after cap, suppress until top-of-next-hour roll-over).
_alert_dedupe = {}                 # f"{kind}:{endpoint}:{ip}" -> unix ts
_hourly_count = defaultdict(int)   # f"{endpoint}:{YYYYMMDDHH}" -> count
_HOURLY_CAP = 5
_DEDUPE_TTL = 300


def _hourly_key(endpoint: str) -> str:
    now_utc = datetime.now(timezone.utc)
    return f"{endpoint}:{now_utc.strftime('%Y%m%d%H')}"


async def send_post_settle_alert(
    kind: str, endpoint: str, ip: str, payer: str, tx_hash: str,
    amount: float, status_code: int, error_summary: str = "",
    tg_send=None,
):
    """Fire-and-forget Telegram alert with dedupe + hourly cap.

    `tg_send` is injected from main.py to avoid circular imports.
    If ENABLE_POST_SETTLE_MONITORING=false → skip telegram (but stats event still logged
    by the caller separately)."""
    if os.getenv("ENABLE_POST_SETTLE_MONITORING", "true").lower() != "true":
        return

    now = time.time()
    expired = [k for k, ts in _alert_dedupe.items() if now - ts > _DEDUPE_TTL]
    for k in expired:
        del _alert_dedupe[k]

    dedupe_key = f"{kind}:{endpoint}:{ip}"
    if dedupe_key in _alert_dedupe:
        return
    _alert_dedupe[dedupe_key] = now

    hkey = _hourly_key(endpoint)
    _hourly_count[hkey] += 1
    if _hourly_count[hkey] > _HOURLY_CAP:
        # Single suppression notice at cap+1; silent thereafter for this hour.
        if _hourly_count[hkey] == _HOURLY_CAP + 1 and tg_send:
            msg = (
                f"🔇 <b>Post-settle alert suppression</b>\n"
                f"엔드포인트: {endpoint}\n"
                f"이번 시간 {_HOURLY_CAP}건 초과 — 매시간 1회 요약만 (다음 시간까지 개별 알림 중지)\n"
                f"시간: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )
            try:
                asyncio.create_task(tg_send(msg))
            except Exception:
                pass
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if kind == "receipt_failed":
        title = "⚠️ <b>영수증 발급 실패 (settle 완료됨)</b>"
    else:
        title = "🚨 <b>SETTLE 후 응답 실패 의심</b>"
    msg = (
        f"{title}\n"
        f"엔드포인트: {endpoint}\n"
        f"status: {status_code}\n"
        f"ip: {ip}\n"
        f"payer: {payer or '?'}\n"
        f"tx_hash: {tx_hash or '?'}\n"
        f"amount: ${amount:.4f}\n"
        f"에러 요약: {error_summary or '?'}\n"
        f"시간: {ts}"
    )
    if tg_send:
        try:
            asyncio.create_task(tg_send(msg))
        except Exception as e:
            print(f"[POST-SETTLE-ALERT] tg_send dispatch failed: {e}")


def log_post_settle_failure(endpoint: str, ip: str, payer: str, tx_hash: str,
                             amount: float, status_code: int, error_summary: str = ""):
    """Record post_settle_failure event in stats.jsonl. Always called; alert is gated separately."""
    try:
        log_event(
            "post_settle_failure",
            endpoint=endpoint.replace("/api/v1/", ""),
            ip=ip,
            payer=payer or "unknown",
            tx_hash=tx_hash or "",
            amount=amount,
            status_code=status_code,
            error_summary=(error_summary or "")[:200],
        )
    except Exception as e:
        print(f"[STATS] post_settle_failure log failed: {e}")


# === Daily summary aggregation =============================================
def aggregate_post_settle_failures(start_ts: int, end_ts: int) -> dict:
    """Read stats.jsonl in [start_ts, end_ts) and aggregate post_settle_failure events.

    Returns:
      {
        "total": N,
        "by_endpoint": {ep: count, ...},
        "unique_payers": M,
        "total_amount_usd": A,
      }
    """
    stats_path = os.getenv(
        "STATS_JSONL_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.jsonl"),
    )
    total = 0
    by_endpoint = defaultdict(int)
    payers = set()
    total_amount = 0.0
    if not os.path.exists(stats_path):
        return {"total": 0, "by_endpoint": {}, "unique_payers": 0, "total_amount_usd": 0.0}
    try:
        with open(stats_path, "r") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "post_settle_failure":
                    continue
                t = e.get("ts", 0)
                if t < start_ts or t >= end_ts:
                    continue
                total += 1
                ep = e.get("endpoint", "unknown")
                by_endpoint[ep] += 1
                p = e.get("payer")
                if p and p != "unknown":
                    payers.add(p)
                total_amount += float(e.get("amount") or 0.0)
    except Exception as ex:
        print(f"[POST-SETTLE-AGG] read error: {ex}")
    return {
        "total": total,
        "by_endpoint": dict(by_endpoint),
        "unique_payers": len(payers),
        "total_amount_usd": round(total_amount, 4),
    }


def render_post_settle_summary_lines(agg: dict) -> str:
    """Build the daily-report line(s) for post_settle_failure section."""
    total = agg["total"]
    if total == 0:
        return "⚠️ Post-settle failures (지난 24h): 0건"
    lines = [f"🚨 Post-settle failures (지난 24h): {total}건"]
    for ep, n in sorted(agg["by_endpoint"].items(), key=lambda x: -x[1]):
        lines.append(f"  {ep}: {n}건")
    lines.append(f"  영향 받은 사용자: {agg['unique_payers']}명")
    lines.append(f"  총 잠재 손실: ${agg['total_amount_usd']:.4f}")
    return "\n".join(lines)


# === MCP daily summary =====================================================
def aggregate_mcp_calls(start_ts: int, end_ts: int) -> dict:
    """Read stats.jsonl in [start_ts, end_ts) and aggregate mcp_call events.

    Returns:
      {
        "total": N,
        "by_tier": {1: a, 2: b, 3: c, 4: d, 5: e, 6: f},
        "by_client": Counter({client_name: count, ...}),
        "by_directory_bot": {bot_name: count, ...},   # Tier 4 breakdown
        "by_generic_http": {client_name: count, ...}, # Tier 5 breakdown
        "new_clients_today": [list of unique UA snippets first seen today],
      }
    """
    stats_path = os.getenv(
        "STATS_JSONL_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.jsonl"),
    )
    total = 0
    by_tier = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
    by_client = defaultdict(int)
    by_directory_bot = defaultdict(int)
    by_generic_http = defaultdict(int)

    # Track UAs seen today vs seen-ever (for "new clients" detection)
    seen_today = set()
    seen_before = set()

    if not os.path.exists(stats_path):
        return {"total": 0, "by_tier": by_tier, "by_client": {},
                "by_directory_bot": {}, "by_generic_http": {}, "new_clients_today": []}
    try:
        with open(stats_path, "r") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("type") != "mcp_call":
                    continue
                t = e.get("ts", 0)
                ua = e.get("user_agent") or ""
                in_window = start_ts <= t < end_ts
                if in_window:
                    total += 1
                    tier = e.get("tier")
                    if isinstance(tier, int) and 1 <= tier <= 6:
                        by_tier[tier] += 1
                    name = e.get("client_name") or "?"
                    by_client[name] += 1
                    ctype = e.get("client_type") or ""
                    if ctype == "directory_bot":
                        by_directory_bot[name] += 1
                    elif ctype in ("generic_http", "unknown"):
                        by_generic_http[name] += 1
                    if ua:
                        seen_today.add(ua)
                else:
                    if ua and t < start_ts:
                        seen_before.add(ua)
    except Exception as ex:
        print(f"[MCP-AGG] read error: {ex}")

    new_clients = sorted(seen_today - seen_before)
    return {
        "total": total,
        "by_tier": by_tier,
        "by_client": dict(by_client),
        "by_directory_bot": dict(by_directory_bot),
        "by_generic_http": dict(by_generic_http),
        "new_clients_today": new_clients,
    }


def render_mcp_summary_lines(agg: dict) -> str:
    """Build the daily-report MCP section. Includes per-tier counts, top-5
    clients, and new-client list."""
    total = agg.get("total", 0)
    if total == 0:
        return "🔌 MCP 호출 (지난 24h): 0건"
    by_tier = agg.get("by_tier", {})
    lines = [f"🔌 <b>MCP 호출 요약 (지난 24h)</b>: 총 {total}건"]
    lines.append(f"  💎 Tier 1 (진성 사용자): {by_tier.get(1, 0)}건")
    lines.append(f"  🔵 Tier 2 (AI 클라이언트): {by_tier.get(2, 0)}건")
    lines.append(f"  🟡 Tier 3 (Agent 프레임워크): {by_tier.get(3, 0)}건")
    lines.append(f"  🟠 Tier 4 (디렉토리 봇): {by_tier.get(4, 0)}건")
    lines.append(f"  ⚪ Tier 5 (일반 HTTP): {by_tier.get(5, 0)}건")
    if by_tier.get(6, 0) > 0:
        lines.append(f"  🔴 Tier 6 (의심): {by_tier[6]}건")

    by_client = agg.get("by_client", {}) or {}
    if by_client:
        top5 = sorted(by_client.items(), key=lambda x: -x[1])[:5]
        lines.append("\n클라이언트 TOP 5:")
        for name, cnt in top5:
            lines.append(f"  - {name}: {cnt}건")

    new_clients = agg.get("new_clients_today", []) or []
    if new_clients:
        lines.append("\n❓ 새 클라이언트 (UA) 발견:")
        for ua in new_clients[:5]:
            lines.append(f"  - {ua[:80]}")
        if len(new_clients) > 5:
            lines.append(f"  ... 외 {len(new_clients) - 5}개")

    return "\n".join(lines)


print(f"[MERCHANT-OPS] loaded; receipt signer = {SIGNER_ADDRESS}")
