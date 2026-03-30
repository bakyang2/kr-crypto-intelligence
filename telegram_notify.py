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
