"""
Backfill IP field into existing stats.jsonl entries (paid api_call only).

Strategy: for each paid api_call event, search systemd journal for the
matching '"GET /api/v1/{endpoint}... 200 OK' line within ±3s of event ts,
extract the source IP, write it back into the event.

Run on Oracle server only (needs journalctl access).
Output: stats.jsonl.with_ip — replace original after verification.
"""

import json
import re
import subprocess
import time
import os
from collections import Counter, defaultdict

STATS_PATH = "/home/ubuntu/KRCryptoAPI/stats.jsonl"
OUT_PATH = "/home/ubuntu/KRCryptoAPI/stats.jsonl.with_ip"

# journalctl line format example:
#   May 04 04:16:07 weatherbot uvicorn[2655444]: INFO:     18.217.112.104:0 - "GET /api/v1/market-read HTTP/1.0" 200 OK
LINE_RE = re.compile(
    r'INFO:\s+(?P<ip>[0-9a-fA-F:.]+):\d+\s+-\s+"GET\s+(?P<path>/api/v1/[^\s?]+)(?:\?[^"]*)?\s+HTTP/[\d.]+"\s+200\s+OK'
)


def journalctl_lines(start_ts: int, end_ts: int):
    """Fetch journalctl lines in [start_ts, end_ts] window."""
    start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts))
    end_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ts))
    try:
        out = subprocess.check_output(
            ["sudo", "journalctl", "-u", "krcryptoapi.service",
             "--since", start_str, "--until", end_str, "--no-pager"],
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).decode("utf-8", errors="replace")
        return out.splitlines()
    except Exception as e:
        print(f"  journalctl error: {e}")
        return []


def find_ip_for_event(ts: int, endpoint: str, journal_cache: dict) -> str | None:
    """Find IP that hit /api/v1/{endpoint} within ±3s of ts.
    journal_cache keyed by 60s buckets to reduce journalctl calls."""
    bucket = ts // 60
    if bucket not in journal_cache:
        # Fetch a 90s window centered on bucket so events near boundaries are covered
        b_start = bucket * 60 - 5
        b_end = (bucket + 1) * 60 + 5
        journal_cache[bucket] = journalctl_lines(b_start, b_end)

    candidates = []
    for line in journal_cache[bucket]:
        m = LINE_RE.search(line)
        if not m:
            continue
        # Path may be "/api/v1/{endpoint}" — endpoint stored without /api/v1/ prefix
        path_endpoint = m.group("path").replace("/api/v1/", "").rstrip("/")
        if path_endpoint != endpoint.rstrip("/"):
            continue
        # Parse timestamp from line: "May 04 04:16:07"
        try:
            ts_str = " ".join(line.split()[:3])
            year = time.localtime(ts).tm_year
            line_ts = int(time.mktime(time.strptime(f"{year} {ts_str}", "%Y %b %d %H:%M:%S")))
        except Exception:
            continue
        if abs(line_ts - ts) <= 3:
            candidates.append((line_ts, m.group("ip")))

    if not candidates:
        return None
    # Pick closest in time
    candidates.sort(key=lambda x: abs(x[0] - ts))
    return candidates[0][1]


def main():
    if not os.path.exists(STATS_PATH):
        print(f"ERROR: {STATS_PATH} not found")
        return

    print(f"Reading {STATS_PATH}...")
    entries = []
    with open(STATS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_raw": line})

    paid_calls = [e for e in entries
                  if isinstance(e, dict)
                  and e.get("type") == "api_call"
                  and e.get("paid") is True]
    print(f"Total entries: {len(entries)}")
    print(f"Paid api_call events: {len(paid_calls)}")
    print(f"Already have ip: {sum(1 for e in paid_calls if e.get('ip'))}")
    print()

    journal_cache = {}
    matched = 0
    skipped_have_ip = 0
    not_found = 0

    for i, e in enumerate(paid_calls):
        if e.get("ip"):
            skipped_have_ip += 1
            continue
        ts = e.get("ts")
        ep = e.get("endpoint", "")
        if not ts or not ep:
            not_found += 1
            continue
        ip = find_ip_for_event(ts, ep, journal_cache)
        if ip:
            e["ip"] = ip
            matched += 1
        else:
            not_found += 1

        if (i + 1) % 25 == 0:
            print(f"  processed {i+1}/{len(paid_calls)} (matched={matched}, missing={not_found})")

    print()
    print("=== Backfill summary ===")
    print(f"Already had ip:        {skipped_have_ip}")
    print(f"Matched from journal:  {matched}")
    print(f"Not found:             {not_found}")
    total = matched + not_found
    if total > 0:
        rate = matched * 100 / total
        print(f"Match rate:            {rate:.1f}%")
    print()

    # Top 10 IPs
    ip_counter = Counter()
    ip_revenue = defaultdict(float)
    for e in paid_calls:
        ip = e.get("ip")
        if ip:
            ip_counter[ip] += 1
            ip_revenue[ip] += e.get("price_usd", 0)
    print("=== Top 10 IPs by call count ===")
    for ip, count in ip_counter.most_common(10):
        print(f"  {ip:<20} {count:>4} calls  ${ip_revenue[ip]:.4f}")
    print()

    # Write output
    print(f"Writing {OUT_PATH}...")
    with open(OUT_PATH, "w") as f:
        for e in entries:
            if "_raw" in e:
                f.write(e["_raw"] + "\n")
            else:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"Done. Review {OUT_PATH}, then:")
    print(f"  cp {STATS_PATH} {STATS_PATH}.bak.$(date +%Y%m%d-%H%M%S)")
    print(f"  mv {OUT_PATH} {STATS_PATH}")


if __name__ == "__main__":
    main()
