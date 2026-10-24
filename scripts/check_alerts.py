"""5분마다: 현재가가 SL·TP·청산가에 가까워지면 텔레그램 알림 (체결 전에 미리)."""
import time
from datetime import datetime, timedelta, timezone

from common import hl_prices, load_json, save_json, telegram

KST = timezone(timedelta(hours=9))
LEVELS = {"정상": 0, "근접": 1, "임박": 2, "청산 위험": 3}
ICON = {"근접": "🟡", "임박": "🔴", "청산 위험": "🚨", "정상": "✅"}


def events(legs, price):
    """가격이 위/아래로 움직일 때 처음 닿는 이벤트."""
    up, down = [], []
    for l in legs:
        sl, tp, liq = l.get("plan_sl"), l.get("plan_tp"), l.get("liq")
        if l["side"] == "long":
            down += [(sl, "SL"), (liq, "청산")]
            up += [(tp, "TP")]
        else:
            up += [(sl, "SL"), (liq, "청산")]
            down += [(tp, "TP")]
    up = [e for e in up if e[0]]
    down = [e for e in down if e[0]]
    u = min(up) if up else None
    d = max(down) if down else None
    return u, d


def main():
    cfg = load_json("alert_config.json", {})
    if not cfg.get("telegram_enabled", True):
        print("알림 꺼짐 (alert_config.json telegram_enabled=false)")
        return
    pos = load_json("positions.json", {}).get("positions", [])
    state = load_json("alert_state.json", {})
    prices = hl_prices()
    now = time.time()
    changed = False
    outbox = []

    accounts = {}
    for p in pos:
        if p["instrument"][:3] in ("XAU", "XAG"):
            accounts.setdefault(p["account"], []).append(p)

    for acct, legs in accounts.items():
        asset = legs[0]["instrument"][:3]
        px = prices.get(asset)
        if not px:
            continue
        th = cfg.get(asset, {"near": 0.015, "imminent": 0.007})
        u, d = events(legs, px)
        cands = []
        if u:
            cands.append(((u[0] - px) / px, "▲", u))
        if d:
            cands.append(((px - d[0]) / px, "▼", d))
        if not cands:
            continue
        dist, arrow, (lv, kind) = min(cands)
        if kind == "청산":
            level = "청산 위험"
        elif dist < th["imminent"]:
            level = "임박"
        elif dist < th["near"]:
            level = "근접"
        else:
            level = "정상"

        prev = state.get(acct, {"level": "정상", "t": 0})
        repeat = cfg.get("repeat_minutes", 60) * 60
        rank, prank = LEVELS[level], LEVELS.get(prev["level"], 0)
        send = (rank > prank) or (rank > 0 and now - prev["t"] >= repeat) or (rank == 0 and prank > 0)
        if send:
            ts = datetime.now(KST).strftime("%m/%d %H:%M")
            if rank == 0:
                msg = f"✅ {acct} 정상 복귀\n{asset} {px:,.3f} · 가장 가까운 이벤트까지 {dist:.1%}"
            else:
                what = {"SL": "SL(손절)", "TP": "TP(익절)", "청산": "청산가"}[kind]
                tail = "\n→ 스왑 격리증거금 보강 필요 (청산가가 SL보다 먼저)" if level == "청산 위험" else \
                       "\n→ 체결되면 짝 다리도 같이 닫혀 OI가 사라져요. 리밸런싱 고려" if kind in ("SL", "TP") else ""
                msg = (f"{ICON[level]} [{level}] {acct} ({ts} KST)\n"
                       f"{asset} 현재 {px:,.3f} → {arrow} {dist:.2%} 앞에 {what} {lv:,.3f}{tail}")
            outbox.append(msg)
            state[acct] = {"level": level, "t": now}
            changed = True
        elif level != prev["level"]:
            state[acct] = {"level": level, "t": prev["t"]}
            changed = True

    if outbox:
        telegram("\n\n".join(outbox))
    if changed:
        save_json("alert_state.json", state)
    print("prices", prices, "changed", changed)


if __name__ == "__main__":
    main()
