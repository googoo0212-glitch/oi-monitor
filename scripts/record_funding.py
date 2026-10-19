"""GitHub Actions가 30분마다 실행: Variational 공개 API에서 금·은 펀딩을 받아 data/에 저장."""
import csv
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
PREFIXES = ("XAU", "XAG")
KEEP_DAYS = 30
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "funding.csv")
JSON_PATH = os.path.join(ROOT, "data", "funding.json")
FIELDS = ["time_utc", "ticker", "contract_type", "mark_price",
          "perp_last_apr", "perp_next_apr", "swap_long_apr", "swap_short_apr", "interval_seconds"]


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "oi-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    rows = []
    for l in data.get("listings", []):
        tk = str(l.get("ticker", ""))
        if not tk.upper().startswith(PREFIXES):
            continue
        fu = l.get("funding") or {}
        last, nxt = fu.get("last") or {}, fu.get("next") or {}
        rows.append({
            "time_utc": now, "ticker": tk, "contract_type": l.get("contract_type", ""),
            "mark_price": f(l.get("mark_price")),
            "perp_last_apr": f(last.get("annualized_rate")), "perp_next_apr": f(nxt.get("annualized_rate")),
            "swap_long_apr": f(last.get("annualized_long_rate")), "swap_short_apr": f(last.get("annualized_short_rate")),
            "interval_seconds": fu.get("interval_seconds"),
        })
    if not rows:
        raise SystemExit("XAU/XAG listings not found")

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)

    # 페이지용 JSON: 최근 KEEP_DAYS일
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:00Z")
    hist = []
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["time_utc"] >= cutoff:
                hist.append({k: (f(v) if k not in ("time_utc", "ticker", "contract_type") else v) for k, v in r.items()})
    with open(JSON_PATH, "w", encoding="utf-8") as fh:
        json.dump(hist, fh, ensure_ascii=False, separators=(",", ":"))
    for r in rows:
        print(r["ticker"], r["perp_next_apr"] if r["perp_next_apr"] is not None else (r["swap_long_apr"], r["swap_short_apr"]))


if __name__ == "__main__":
    main()
