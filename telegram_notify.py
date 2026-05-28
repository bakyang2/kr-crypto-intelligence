import os
import httpx
import time

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_last_summary_time = 0
_pending_requests = []

async def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
            )
    except Exception:
        pass

async def notify_startup():
    await send_message("🟢 <b>KR Crypto API</b> 서버 시작됨\nhttps://api.printmoneylab.com/health")

async def notify_request(endpoint, symbol, ip):
    global _last_summary_time, _pending_requests
    _pending_requests.append({
        "endpoint": endpoint,
        "symbol": symbol,
        "ip": ip,
        "time": time.strftime("%H:%M:%S")
    })
    # 1분마다 요약 전송 (알림 폭탄 방지)
    now = time.time()
    if now - _last_summary_time >= 60 and _pending_requests:
        count = len(_pending_requests)
        endpoints = {}
        for r in _pending_requests:
            key = r["endpoint"]
            endpoints[key] = endpoints.get(key, 0) + 1
        summary = "\n".join([f"  {k}: {v}건" for k, v in endpoints.items()])
        unique_ips = len(set(r["ip"] for r in _pending_requests))
        await send_message(
            f"📊 <b>최근 1분 요약</b>\n"
            f"총 {count}건 | IP {unique_ips}개\n{summary}"
        )
        _pending_requests.clear()
        _last_summary_time = now

async def notify_daily_summary(stats):
    await send_message(
        f"📈 <b>일일 요약</b> ({stats.get('today_date', '')})\n"
        f"오늘 요청: {stats.get('today_requests', 0)}건\n"
        f"누적 요청: {stats.get('total_requests', 0)}건\n"
        f"에러: {stats.get('errors', 0)}건"
    )


async def _send_post_settle_alert(kind: str, endpoint: str, ip: str,
                                  payer: str, tx_hash: str,
                                  amount: float, status_code: int,
                                  error_summary: str = ""):
    """Thin wrapper around merchant_ops.send_post_settle_alert that uses this
    module's send_message for Telegram delivery.

    Kept here so callers without a tg_send injection can still emit the alert
    (e.g. test fixtures, external scripts). The production path in main.py
    routes through merchant_ops with tg_send injected directly."""
    from merchant_ops import send_post_settle_alert as _impl
    await _impl(
        kind=kind, endpoint=endpoint, ip=ip, payer=payer, tx_hash=tx_hash,
        amount=amount, status_code=status_code, error_summary=error_summary,
        tg_send=send_message,
    )


async def _send_mcp_alert(tier: int, client_name: str, tool_name: str,
                          ip: str, user_agent: str, details: dict):
    """Stable Telegram entry point for MCP-classification alerts.

    Production path lives in mcp_server.py (which calls _tg_notify directly to
    avoid an import cycle with the merchant_ops/main bundle). This wrapper
    exists so external callers — backfill scripts, tests, future re-routers —
    can dispatch the same formatted alert through this module's send_message."""
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    ts = datetime.now(KST).strftime("%H:%M:%S KST")
    country = details.get("country", "")
    cc = f" {country}" if country else ""

    if tier in (1, 2, 3):
        emoji = {1: "💎", 2: "🔵", 3: "🟡"}[tier]
        label = {1: "PAID USER", 2: "AI CLIENT", 3: "AGENT FRAMEWORK"}[tier]
        body = (
            f"{emoji} <b>MCP 호출 [{label}]</b>\n"
            f"Tool: {tool_name}\nClient: {client_name}\nIP: {ip}{cc}\n"
        )
        if tier == 1:
            body += f"24h 결제: {details.get('recent_payment_count_24h', 0)}건\n"
        if tier in (2, 3):
            body += ("결제 헤더: " + ("있음" if details.get("has_payment_header") else "없음 (사용 시도 추정)") + "\n")
        if user_agent:
            body += f"User-Agent: {user_agent[:80]}\n"
        body += f"시간: {ts}"
        await send_message(body)
    elif tier == 6:
        await send_message(
            f"🔴 <b>의심 활동 감지</b>\nIP: {ip}{cc}\n"
            f"User-Agent: {(user_agent or '<empty>')[:80]}\n"
            f"권장: 차단 검토\n시간: {ts}"
        )
    # Tier 4 / 5 are silent (daily summary only)
