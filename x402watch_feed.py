"""
KR Crypto-side x402watch merchant feed endpoint (reference implementation).

Permanent location on KR Crypto Oracle:
  /home/ubuntu/KRCryptoAPI/app/x402watch_feed.py

Mount from KR Crypto's existing FastAPI app:
  from app.x402watch_feed import router as feed_router
  app.include_router(feed_router)

Endpoint:
  GET /.well-known/x402watch-feed.json
  GET /api/v1/x402watch-feed.json
    (both serve the same handler — .well-known is the canonical path)

Data source:
  Reads /home/ubuntu/KRCryptoAPI/stats.jsonl, filters payment_settled
  rows in the requested time window, normalises to the feed spec
  shape, signs with the merchant's Ed25519 key.

Required env:
  KR_FEED_PRIVATE_KEY_PATH        path to PEM-encoded Ed25519 private key
  KR_FEED_PUBLIC_KEY_ID           e.g. "kr-crypto-feed-2026-05-19"
  KR_FEED_MERCHANT_ID             e.g. "kr-crypto"

Dependencies:
  pip install cryptography fastapi pydantic
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger("x402watch_feed")

FEED_VERSION = 1
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
DEFAULT_WINDOW_HOURS = 24

# KR Crypto's seller wallets — keep in sync with what x402watch sees.
SELLER_ADDRESSES = [
    "0xcF9223eCe895258dEa8D288AEBcf846Ab8E342fB",   # Base + Polygon EVM
    "3Ywxk31SvWKwZBdY6bLvjmn5h4mzWcT3HJ5UZbYXoVy9",  # Solana
]

STATS_PATH = Path(os.environ.get("KR_STATS_PATH", "/home/ubuntu/KRCryptoAPI/stats.jsonl"))


router = APIRouter()


# ─── Helpers ────────────────────────────────────────────────────────
def _load_signing_key() -> Ed25519PrivateKey:
    path = os.environ.get("KR_FEED_PRIVATE_KEY_PATH", "")
    if not path:
        raise RuntimeError("KR_FEED_PRIVATE_KEY_PATH not set")
    with open(path, "rb") as f:
        data = f.read()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("Expected Ed25519 private key in PEM")
    return key


_signing_key: Optional[Ed25519PrivateKey] = None


def _signing_key_lazy() -> Ed25519PrivateKey:
    global _signing_key
    if _signing_key is None:
        _signing_key = _load_signing_key()
    return _signing_key


def _canonical_json(obj: Any) -> bytes:
    """JCS-compatible canonical JSON: sorted keys, no whitespace, NFC strings.
    For the merchant feed scope we don't need full NFC normalisation since
    we control the inputs, but sorted keys + (',', ':') separators are
    sufficient for deterministic signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sign(body_without_sig: dict) -> dict:
    msg = _canonical_json(body_without_sig)
    sig = _signing_key_lazy().sign(msg)
    return {
        "alg": "Ed25519",
        "key_id": os.environ.get("KR_FEED_PUBLIC_KEY_ID", "kr-crypto-feed"),
        "value": base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii"),
    }


def _parse_since(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now(timezone.utc) - timedelta(hours=DEFAULT_WINDOW_HOURS)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "invalid `since` ISO timestamp")


def _norm_chain(network: str) -> str:
    """Normalise legacy chain labels to CAIP-2 for the feed."""
    n = (network or "").lower()
    if n in ("base", "eip155:8453", "8453"):
        return "eip155:8453"
    if n in ("polygon", "eip155:137", "137"):
        return "eip155:137"
    if n in ("solana", "sol"):
        return "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    return network


def _norm_resource_url(endpoint: str) -> str:
    """Map a stats.jsonl `endpoint` value to the canonical resource_url
    advertised on Bazaar. KR Crypto's stats.jsonl typically records
    something like 'kr-prices' or '/api/v1/kr-prices' — both map to
    the same canonical URL."""
    if endpoint.startswith("http"):
        return endpoint
    path = endpoint if endpoint.startswith("/") else f"/api/v1/{endpoint.lstrip('/')}"
    return f"https://api.printmoneylab.com{path}"


def _amount_to_base_units(price_usd: float) -> str:
    """USDC has 6 decimals on both Base and Solana."""
    if price_usd is None:
        return "0"
    return str(int(round(price_usd * 1_000_000)))


def _iter_payments(stats_path: Path, since: datetime) -> Iterable[dict]:
    """Stream payment_settled rows from stats.jsonl whose settlement
    timestamp is >= since. Tolerates malformed lines."""
    if not stats_path.exists():
        log.warning("stats.jsonl missing: %s", stats_path)
        return
    with stats_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (row.get("type") or row.get("kind") or row.get("event")) not in ("payment_settled", "payment", "settled"):
                # Accept several event names KR Crypto might have used.
                continue
            ts_raw = row.get("ts") or row.get("settled_at") or row.get("time")
            if not ts_raw:
                continue
            try:
                if isinstance(ts_raw, (int, float)):
                    ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
                else:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since:
                continue
            yield row


def _next_feed_seq() -> int:
    """Monotonically increasing per-merchant sequence number, persisted
    to a small file on disk so it survives restarts."""
    seq_path = Path(os.environ.get(
        "KR_FEED_SEQ_PATH",
        "/home/ubuntu/KRCryptoAPI/var/x402watch_feed_seq",
    ))
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = int(seq_path.read_text().strip())
    except Exception:
        cur = 0
    nxt = cur + 1
    seq_path.write_text(str(nxt))
    return nxt


# ─── Route ──────────────────────────────────────────────────────────
@router.get("/.well-known/x402watch-feed.json")
@router.get("/api/v1/x402watch-feed.json")
async def x402watch_feed(
    since: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(default=None),
) -> dict:
    since_dt = _parse_since(since)
    until_dt = datetime.now(timezone.utc)

    # Cursor encoding: base64url(JSON({"after_ts": ISO})). We use
    # streaming + cursor instead of offset to stay stable under writes.
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "==").decode())
            cursor_ts = datetime.fromisoformat(decoded["after_ts"].replace("Z", "+00:00"))
            if cursor_ts > since_dt:
                since_dt = cursor_ts
        except Exception:
            raise HTTPException(400, "invalid cursor")

    settlements = []
    last_ts: Optional[datetime] = None
    truncated = False
    for row in _iter_payments(STATS_PATH, since_dt):
        ts_raw = row.get("ts") or row.get("settled_at") or row.get("time")
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        else:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts > until_dt:
            continue
        chain = _norm_chain(row.get("network") or row.get("chain") or "")
        seller = row.get("payTo") or row.get("seller") or row.get("merchant_wallet")
        if not seller:
            # Infer from chain if possible.
            seller = SELLER_ADDRESSES[1] if chain.startswith("solana") else SELLER_ADDRESSES[0]
        payer = row.get("payer") or row.get("buyer") or row.get("from_address") or ""
        endpoint = row.get("endpoint") or row.get("resource") or row.get("path") or ""
        price = float(row.get("price_usd") or row.get("amount_usd") or 0)
        tx_hash = row.get("transaction") or row.get("tx_hash") or row.get("signature")
        if not tx_hash:
            continue

        settlements.append(OrderedDict([
            ("tx_hash", tx_hash),
            ("chain", chain),
            ("settled_at", ts.isoformat().replace("+00:00", "Z")),
            ("resource_url", _norm_resource_url(endpoint)),
            ("payer", payer),
            ("seller", seller),
            ("amount_usdc", _amount_to_base_units(price)),
            ("price_usd", price),
            ("x402_version", int(row.get("x402_version") or 2)),
        ]))
        last_ts = ts
        if len(settlements) >= limit:
            truncated = True
            break

    next_cursor = None
    if truncated and last_ts is not None:
        cursor_obj = {"after_ts": last_ts.isoformat().replace("+00:00", "Z")}
        next_cursor = base64.urlsafe_b64encode(
            _canonical_json(cursor_obj)
        ).rstrip(b"=").decode("ascii")

    body = OrderedDict([
        ("feed_version", FEED_VERSION),
        ("merchant_id", os.environ.get("KR_FEED_MERCHANT_ID", "kr-crypto")),
        ("seller_addresses", list(SELLER_ADDRESSES)),
        ("feed_seq", _next_feed_seq()),
        ("issued_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
        ("window", OrderedDict([
            ("since", since_dt.isoformat().replace("+00:00", "Z")),
            ("until", until_dt.isoformat().replace("+00:00", "Z")),
        ])),
        ("settlements", settlements),
    ])
    if next_cursor:
        body["next_cursor"] = next_cursor

    # Sign over the body *without* the signature field.
    body["signature"] = _sign(body)

    return body


# ─── Key-management bootstrap (one-off) ──────────────────────────────
# Run once to generate the key pair. Not part of the live route.
#
#   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
#   from cryptography.hazmat.primitives import serialization
#   import base64
#   k = Ed25519PrivateKey.generate()
#   pem = k.private_bytes(
#       encoding=serialization.Encoding.PEM,
#       format=serialization.PrivateFormat.PKCS8,
#       encryption_algorithm=serialization.NoEncryption(),
#   )
#   with open("/home/ubuntu/KRCryptoAPI/secrets/x402watch_feed.ed25519.pem", "wb") as f:
#       f.write(pem)
#   pub_raw = k.public_key().public_bytes(
#       encoding=serialization.Encoding.Raw,
#       format=serialization.PublicFormat.Raw,
#   )
#   print("public_key_b64url:", base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode())
