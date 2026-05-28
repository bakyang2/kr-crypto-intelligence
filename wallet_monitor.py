#!/usr/bin/env python3
"""
wallet_monitor.py — Poll watched bot wallets' USDC balance on Base mainnet.
On balance increase >= TOPUP_THRESHOLD_USDC, send a Telegram alert.

Standalone cron script. Does not import or modify main.py.
Telegram credentials read from .env (cron does not source bash profile).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "wallet_balance_cache.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")


def _load_env():
    """Load KEY=VALUE lines from .env into os.environ (cron-safe)."""
    if not os.path.exists(ENV_PATH):
        return
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception as e:
        print(f"[WALLET-MON] .env load error: {e}")


_load_env()

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# 모니터링 지갑 (주소 → 라벨). 추가 시 dict에 항목만 추가하면 됨.
WATCHED_WALLETS = {
    "0x15C3cDD668c6c8DC0d9F0E2b9DDE14d9A1EcbC2B": "GCP SG (김프 차익봇)",
}

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base mainnet
RPC_URL = "https://mainnet.base.org"
TOPUP_THRESHOLD_USDC = 1.0  # 충전 알림 임계값 ($)
USDC_DECIMALS = 6


async def fetch_usdc_balance(client: httpx.AsyncClient, wallet_addr: str) -> float:
    """USDC.balanceOf(wallet) on Base via JSON-RPC eth_call.
    Returns balance in USDC (decimal). Raises on RPC error."""
    addr_no0x = wallet_addr.lower().replace("0x", "")
    # balanceOf(address) selector + 32-byte left-padded address
    data = "0x70a08231" + ("0" * 24) + addr_no0x
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": USDC_BASE, "data": data}, "latest"],
        "id": 1,
    }
    r = await client.post(RPC_URL, json=payload, timeout=15)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    raw = int(body.get("result", "0x0"), 16)
    return raw / (10 ** USDC_DECIMALS)


def load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"[WALLET-MON] cache load error: {e}")
        return {}


def save_cache(cache: dict):
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        print(f"[WALLET-MON] cache save error: {e}")


async def tg_send(client: httpx.AsyncClient, text: str):
    """Send Telegram message via the same bot KR Crypto API uses.
    No-op if creds missing."""
    if not TG_TOKEN or not TG_CHAT:
        print(f"[WALLET-MON] TG creds missing; would send: {text[:120]}")
        return
    try:
        await client.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WALLET-MON] TG send error: {e}")


def _short(addr: str) -> str:
    return addr[:10] + "..." + addr[-8:]


async def main():
    cache = load_cache()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    async with httpx.AsyncClient() as client:
        for addr, label in WATCHED_WALLETS.items():
            try:
                balance = await fetch_usdc_balance(client, addr)
            except Exception as e:
                print(f"[{ts_str}] [WALLET-MON] balance fetch error for {label} ({addr}): {e}")
                continue

            prev_entry = cache.get(addr)
            prev_balance = prev_entry.get("balance_usdc") if isinstance(prev_entry, dict) else None

            if prev_balance is None:
                print(f"[{ts_str}] [WALLET-MON] init {label}: ${balance:.4f} USDC (cache seeded, no alert)")
            else:
                delta = balance - prev_balance
                if delta >= TOPUP_THRESHOLD_USDC:
                    msg = (
                        f"💰 <b>봇 지갑 충전 감지!</b>\n"
                        f"운영자: {label}\n"
                        f"이전: ${prev_balance:.4f} USDC\n"
                        f"현재: ${balance:.4f} USDC\n"
                        f"증가: +${delta:.4f}\n"
                        f"기대: 결제 자동 재개 가능\n"
                        f"지갑: {_short(addr)}"
                    )
                    await tg_send(client, msg)
                    print(f"[{ts_str}] [WALLET-MON] TOPUP {label}: +${delta:.4f} → ${balance:.4f}")
                else:
                    print(f"[{ts_str}] [WALLET-MON] {label}: ${balance:.4f} USDC (Δ {delta:+.4f}, no alert)")

            cache[addr] = {"balance_usdc": balance, "last_checked_ts": now_ts}

    save_cache(cache)


if __name__ == "__main__":
    asyncio.run(main())
