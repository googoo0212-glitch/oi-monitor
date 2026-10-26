"""5분마다: 계정 내 페어(CL/BZ, 금/은)의 '청산 여유 소진율'을 계산해 텔레그램 알림.

이 구조는 SL/TP 없이 운영해요. 위험은 두 다리의 가격 '차이'(BZ−CL, 금÷은)뿐이라서
계정별로 처음 여유(현금 − 유지증거금) 중 얼마나 써버렸는지로 단계를 나눠요.
  소진 35% → 리밸런싱 / 55% → 즉시 송금 / 75% → 비상(두 다리 같이 정리 고려)
가격은 Variational mark price가 기본 (청산도 이 가격 기준). 실패하면 하이퍼리퀴드 xyz 중간가로 대체.
실행할 때마다 data/pair_marks.csv 에 가격을 한 줄씩 남겨서 페이지에서 가격차 추이를 그려요.
"""
import os
import time
from datetime import datetime, timedelta, timezone

from common import DATA, load_json, save_json, telegram, variational_marks, xyz_mids

money = lambda v: f"{'−' if v < 0 else '+'}${abs(v):,.0f}"

KST = timezone(timedelta(hours=9))
SYM = {"CL-PERP": "xyz:CL", "BZ-PERP": "xyz:BRENTOIL", "XAU-PERP": "xyz:GOLD", "XAG-PERP": "xyz:SILVER"}
LEVELS = ["정상", "리밸런싱", "즉시 송금", "비상"]
ICON = {"정상": "✅", "리밸런싱": "🟡", "즉시 송금": "🔴", "비상": "🚨"}


def account_state(name, acct, legs, px):
    pnl = sum((1 if l["side"] == "long" else -1) * l["qty"] * (px[l["instrument"]] - l["entry"]) for l in legs)
    oi_now = sum(l["qty"] * px[l["instrument"]] for l in legs)
    oi_0 = sum(l["qty"] * l["entry"] for l in legs)
    mm_now, mm_0 = acct["mm_rate"] * oi_now, acct["mm_rate"] * oi_0
    equity = acct["cash"] + pnl
    buf0, buf = acct["cash"] - mm_0, equity - mm_now
    consumed = 1 - buf / buf0 if buf0 > 0 else 1
    leg = max(l["qty"] * px[l["instrument"]] for l in legs)
    return {"pnl": pnl, "equity": equity, "mm_usage": mm_now / equity if equity > 0 else 9,
            "consumed": consumed, "room": buf / leg}


def log_marks(px, src):
    path = os.path.join(DATA, "pair_marks.csv")
    new = not os.path.exists(path)
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("time_utc,CL,BZ,XAU,XAG,src\n")
        fh.write(",".join([t] + [f"{px[k]:.4f}" if k in px else "" for k in SYM] + [src]) + "\n")


def describe(legs, px):
    inst = {l["instrument"] for l in legs}
    if {"CL-PERP", "BZ-PERP"} <= inst:
        return f"가격차 BZ−CL ${px['BZ-PERP'] - px['CL-PERP']:.2f}"
    if {"XAU-PERP", "XAG-PERP"} <= inst:
        return f"금÷은 {px['XAU-PERP'] / px['XAG-PERP']:.2f}"
    return ""


def main():
    cfg = load_json("alert_config.json", {})
    if not cfg.get("telegram_enabled", True):
        print("알림 꺼짐 (alert_config.json telegram_enabled=false)")
        return
    th = cfg.get("pair_levels", {"rebalance": 0.35, "urgent": 0.55, "emergency": 0.75})
    data = load_json("positions.json", {})
    accts, pos = data.get("accounts", {}), data.get("positions", [])
    if not pos:
        print("포지션 없음")
        return
    px, src, vm_keys = {}, "variational", set()
    try:
        vm = variational_marks()
        vm_keys = {k for k in SYM if k in vm}
        px = {k: vm[k] for k in SYM if k in vm}
    except Exception as e:
        print("Variational 가격 실패:", e)
    if len(px) < len(SYM):
        try:
            mids = xyz_mids()
            for k, v in SYM.items():
                if k not in px and v in mids:
                    px[k] = mids[v]
        except Exception as e:
            print("하이퍼리퀴드 가격 실패:", e)
    if src == "variational" and len(px) < len(SYM):
        src = "hyperliquid" if not any(k in vm_keys for k in px) else "mixed"
    log_marks(px, src)
    state = load_json("alert_state.json", {})
    now, changed, outbox = time.time(), False, []
    by = {}
    for p in pos:
        by.setdefault(p["account"], []).append(p)
    st = {}
    for name, legs in by.items():
        if name not in accts or any(l["instrument"] not in px for l in legs):
            continue
        st[name] = account_state(name, accts[name], legs, px)
    for name, s in st.items():
        c = s["consumed"]
        level = "비상" if c >= th["emergency"] else "즉시 송금" if c >= th["urgent"] else "리밸런싱" if c >= th["rebalance"] else "정상"
        prev = state.get(name, {"level": "정상", "t": 0})
        if prev.get("level") not in LEVELS:
            prev = {"level": "정상", "t": 0}
        rank, prank = LEVELS.index(level), LEVELS.index(prev["level"])
        repeat = cfg.get("repeat_minutes", 60) * 60
        send = rank > prank or (rank > 0 and now - prev["t"] >= repeat) or (rank == 0 and prank > 0)
        if send:
            ts = datetime.now(KST).strftime("%m/%d %H:%M")
            a = accts[name]
            mates = [n for n, x in accts.items() if x["pair"] == a["pair"] and x["role"] != a["role"] and n in st]
            if rank == 0:
                msg = f"✅ {name} 정상 복귀 · {describe(by[name], px)} · 여유 소진 {c:.0%}"
            else:
                need = max(0.0, -s["pnl"]) * 0.5
                gain = sum(max(0.0, st[m]["pnl"]) for m in mates) or 1
                tx = " / ".join(f"{m} → {name} ${need * max(0.0, st[m]['pnl']) / gain:,.0f}" for m in mates)
                tail = {"리밸런싱": f"\n→ 송금 권장: {tx}",
                        "즉시 송금": f"\n→ 지금 송금: {tx}",
                        "비상": f"\n→ 송금이 어려우면 {name}의 두 다리를 같은 수량으로 같이 줄이기"}[level]
                msg = (f"{ICON[level]} [{level}] {name} ({ts} KST)\n"
                       f"{describe(by[name], px)} · 손익 {money(s['pnl'])} · 여유 소진 {c:.0%}"
                       f" · 유지증거금 사용률 약 {s['mm_usage']:.0%} · 청산까지 약 {s['room']:.1%}{tail}")
            outbox.append(msg)
            state[name] = {"level": level, "t": now}
            changed = True
        elif level != prev["level"]:
            state[name] = {"level": level, "t": prev["t"]}
            changed = True
    for k in [k for k in state if k not in st]:
        del state[k]
        changed = True
    if outbox:
        telegram("\n\n".join(outbox))
    if changed:
        save_json("alert_state.json", state)
    print({n: f"소진 {s['consumed']:.0%} 여유 {s['room']:.1%}" for n, s in st.items()})


if __name__ == "__main__":
    main()
