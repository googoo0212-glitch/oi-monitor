"""GitHub Actions가 30분마다 실행: Variational 공개 API에서 금·은 펀딩을 받아 data/에 저장."""
import csv
import sys
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
PREFIXES = ("XAU", "XAG", "PAXG")
KEEP_DAYS = 30
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    # 지금 보유 중인 다른 종목(예: UNI-PERP)도 같이 기록
    EXTRA = set()
    pp = os.path.join(ROOT, "data", "positions.json")
    if os.path.exists(pp):
        with open(pp, encoding="utf-8") as fh:
            EXTRA = {p["instrument"] for p in json.load(fh).get("positions", [])}
    rows = []
    for l in data.get("listings", []):
        tk = str(l.get("ticker", ""))
        if not (tk.upper().startswith(PREFIXES) or tk in EXTRA):
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
    funding_alert(hist)


def funding_alert(hist):
    """은 퍼프 펀딩이 크게 바뀌거나 은 S 순펀딩이 마이너스로 돌아서면 텔레그램."""
    try:
        from common import load_json, telegram
    except ImportError:
        return
    cfg = load_json("alert_config.json", {}).get("funding", {"change_pp": 0.03, "watch": ["XAG-PERP"]})
    by = {}
    for r in hist:
        by.setdefault(r["ticker"], []).append(r)
    apr = lambda r: r["perp_next_apr"] if r["perp_next_apr"] is not None else r["perp_last_apr"]
    msgs = []
    for tk in cfg.get("watch", []):
        rs = by.get(tk, [])
        if len(rs) < 2 or apr(rs[-1]) is None or apr(rs[-2]) is None:
            continue
        a, b = apr(rs[-2]), apr(rs[-1])
        if abs(b - a) >= cfg.get("change_pp", 0.03):
            msgs.append(f"{tk} 펀딩 {a:+.2%} → {b:+.2%}")
    xp, xs = by.get("XAG-PERP", []), by.get("XAG-SWAP", [])
    if len(xp) >= 2 and xs:
        long_pay = xs[-1]["swap_long_apr"] or 0
        n0, n1 = (apr(xp[-2]) or 0) - long_pay, (apr(xp[-1]) or 0) - long_pay
        if n0 >= 0 > n1:
            msgs.append(f"⚠ 은 S 순펀딩이 마이너스로 전환: {n1:+.2%} (퍼프 숏 수령 < 스왑 롱 지불)")
        elif n0 < 0 <= n1:
            msgs.append(f"은 S 순펀딩 플러스 복귀: {n1:+.2%}")
    if msgs:
        telegram("💸 펀딩 변화\n" + "\n".join(msgs))


if __name__ == "__main__":
    main()
