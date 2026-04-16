"""
stats_logger.py — JSONL event logging + aggregation for KR Crypto Intelligence
"""

import json
import time
import os

STATS_FILE = os.getenv("STATS_JSONL_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.jsonl"))
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_archive")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def log_event(event_type: str, **kwargs):
    """Append one JSON event to stats.jsonl"""
    try:
        event = {"ts": int(time.time()), "type": event_type, **kwargs}
        with open(STATS_FILE, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[STATS] Log failed: {e}")


def aggregate_stats(since_ts: int) -> dict:
    """Aggregate events since given unix timestamp"""
    events = _read_events_since(since_ts)
    if not events:
        return {
            "api_calls_total": 0, "cache_hits": 0, "claude_calls": 0,
            "paid_calls": 0, "revenue_usd": 0.0, "claude_cost_usd": 0.0,
            "alerts_sent": 0, "errors": 0, "by_endpoint": {},
        }

    api_calls = [e for e in events if e["type"] == "api_call"]
    claude_calls = [e for e in events if e["type"] == "claude_call"]

    return {
        "api_calls_total": len(api_calls),
        "cache_hits": len([e for e in events if e["type"] == "cache_hit"]),
        "claude_calls": len(claude_calls),
        "paid_calls": len([e for e in api_calls if e.get("paid")]),
        "revenue_usd": round(sum(e.get("price_usd", 0) for e in api_calls if e.get("paid")), 4),
        "claude_cost_usd": round(sum(e.get("cost_usd", 0) for e in claude_calls), 6),
        "alerts_sent": len([e for e in events if e["type"] == "alert_sent"]),
        "errors": len([e for e in events if e["type"] == "error"]),
        "by_endpoint": _group_by_endpoint(events),
    }


def aggregate_stats_range(start_ts: int, end_ts: int) -> dict:
    """Aggregate events in [start_ts, end_ts) range"""
    events = _read_events_since(start_ts)
    events = [e for e in events if e["ts"] < end_ts]
    if not events:
        return aggregate_stats(int(time.time()))  # empty result

    api_calls = [e for e in events if e["type"] == "api_call"]
    claude_calls = [e for e in events if e["type"] == "claude_call"]

    return {
        "api_calls_total": len(api_calls),
        "cache_hits": len([e for e in events if e["type"] == "cache_hit"]),
        "claude_calls": len(claude_calls),
        "paid_calls": len([e for e in api_calls if e.get("paid")]),
        "revenue_usd": round(sum(e.get("price_usd", 0) for e in api_calls if e.get("paid")), 4),
        "claude_cost_usd": round(sum(e.get("cost_usd", 0) for e in claude_calls), 6),
        "alerts_sent": len([e for e in events if e["type"] == "alert_sent"]),
        "errors": len([e for e in events if e["type"] == "error"]),
        "by_endpoint": _group_by_endpoint(events),
    }


def _read_events_since(since_ts: int) -> list:
    events = []
    if not os.path.exists(STATS_FILE):
        return events
    try:
        with open(STATS_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("ts", 0) >= since_ts:
                        events.append(e)
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        print(f"[STATS] Read failed: {e}")
    return events


def _group_by_endpoint(events: list) -> dict:
    result = {}
    for e in events:
        if e["type"] in ("api_call", "claude_call", "cache_hit"):
            ep = e.get("endpoint", "unknown")
            if ep not in result:
                result[ep] = {"api": 0, "claude": 0, "cache_hits": 0, "cost": 0.0, "revenue": 0.0}
            if e["type"] == "api_call":
                result[ep]["api"] += 1
                if e.get("paid"):
                    result[ep]["revenue"] += e.get("price_usd", 0)
            elif e["type"] == "claude_call":
                result[ep]["claude"] += 1
                result[ep]["cost"] += e.get("cost_usd", 0)
            elif e["type"] == "cache_hit":
                result[ep]["cache_hits"] += 1
    return result


def maybe_archive():
    """Archive stats.jsonl if >10MB or has events older than 30 days"""
    try:
        if not os.path.exists(STATS_FILE):
            return
        size = os.path.getsize(STATS_FILE)
        if size < MAX_FILE_SIZE:
            return
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        archive_name = f"stats_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        archive_path = os.path.join(ARCHIVE_DIR, archive_name)
        os.rename(STATS_FILE, archive_path)
        print(f"[STATS] Archived to {archive_path} ({size / 1024 / 1024:.1f}MB)")
    except Exception as e:
        print(f"[STATS] Archive failed: {e}")


print("[STATS] stats_logger module loaded")
