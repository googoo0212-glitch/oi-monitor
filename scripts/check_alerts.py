"""5분마다: 계정 내 페어(CL/BZ, 금/은)의 '청산 여유 소진율'을 계산해 텔레그램 알림.

이 구조는 SL/TP 없이 운영해요. 위험은 두 다리의 가격 '차이'(BZ−CL, 금÷은)뿐이라서
계정별로 처음 여유(현금 − 유지증거금) 중 얼마나 써버렸는지로 단계를 나눠요.
  소진 35% → 리밸런싱 / 55% → 즉시 송금 / 75% → 비상(두 다리 같이 정리 고려)
가격은 Variational mark price가 기본 (청산도 이 가격 기준). 실패하면 하이퍼리퀴드 xyz 중간가로 대체.
실행할 때마다 data/pair_marks.csv 에 가격을 한 줄씩 남겨서 페이지에서 가격차 추이를 그려요.
매일 아침 9시(KST) 이후 첫 실행에서 일일 요약을 한 번 보내요 (Entropy 요약과 같은 형식).
"""
import csv
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


def pair_view(legs, acct, px):
    """가격차(원유 BZ−CL) 또는 비율(금÷은): 진입값, 지금, 소진 f가 되는 값, 손해 방향."""
    by = {l["instrument"]: l for l in legs}
    mm0 = acct["mm_rate"] * sum(l["qty"] * l["entry"] for l in legs)
    buf0 = acct["cash"] - mm0
    if "CL-PERP" in by:
        e, c = by["BZ-PERP"]["entry"] - by["CL-PERP"]["entry"], px["BZ-PERP"] - px["CL-PERP"]
        unit, up = buf0 / by["CL-PERP"]["qty"], by["CL-PERP"]["side"] == "long"
        return {"oil": True, "entry": e, "cur": c, "up": up, "at": lambda f: e + f * unit if up else e - f * unit}
    e, c = by["XAU-PERP"]["entry"] / by["XAG-PERP"]["entry"], px["XAU-PERP"] / px["XAG-PERP"]
    unit, up = e * buf0 / (by["XAU-PERP"]["qty"] * by["XAU-PERP"]["entry"]), by["XAU-PERP"]["side"] == "short"
    return {"oil": False, "entry": e, "cur": c, "up": up, "at": lambda f: e + f * unit if up else e - f * unit}


def day_range(col_a, col_b, oil):
    """최근 24시간 가격차(비율) 최저~최고 (pair_marks.csv)."""
    path = os.path.join(DATA, "pair_marks.csv")
    if not os.path.exists(path):
        return None
    cut = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:00Z")
    vals = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["time_utc"] >= cut and r[col_a] and r[col_b]:
                a, b = float(r[col_a]), float(r[col_b])
                vals.append(b - a if oil else a / b)
    return (min(vals), max(vals)) if vals else None


def daily_report(accts, by, st, px, src, th):
    fmt = lambda oil, v: f"${v:.2f}" if oil else f"{v:.2f}"
    ts = datetime.now(KST).strftime("%m/%d %H:%M")
    oi = sum(l["qty"] * px[l["instrument"]] for legs in by.values() for l in legs)
    eq = sum(s["equity"] for s in st.values())
    pnl = sum(s["pnl"] for s in st.values())
    lines = [f"📊 Variational 일일 요약 ({ts} KST)", "",
             f"🧱 OI ${oi:,.0f} · 총자산 ${eq:,.0f} · 손익 {money(pnl)}", "",
             "📏 짝별 가격차 (진입 → 지금 · 송금 기준까지)"]
    todo = []
    for pid in sorted({a["pair"] for a in accts.values()}):
        names = [n for n, a in accts.items() if a["pair"] == pid and n in st]
        if not names:
            continue
        views = {n: pair_view(by[n], accts[n], px) for n in names}
        v0 = next(iter(views.values()))
        oil = v0["oil"]
        entry = sum(v["entry"] for v in views.values()) / len(views)
        cur = v0["cur"]
        ups = [v["at"](th["rebalance"]) for v in views.values() if v["up"]]
        dns = [v["at"](th["rebalance"]) for v in views.values() if not v["up"]]
        worst = max(names, key=lambda n: st[n]["consumed"])
        c = st[worst]["consumed"]
        icon = "🔴" if c >= th["urgent"] else "🟠" if c >= th["rebalance"] else "🟡" if c >= 0.25 else "✅"
        mv = cur - entry
        mvs = f"{'+' if mv >= 0 else '−'}${abs(mv):.2f}" if oil else f"{mv / entry:+.1%}"
        gap_u = f"↑{fmt(oil, min(ups) - cur)}" if ups else ""
        gap_d = f"↓{fmt(oil, cur - max(dns))}" if dns else ""
        rng = day_range("CL", "BZ", True) if oil else day_range("XAU", "XAG", False)
        lines.append(f"{icon} 짝{pid} {'원유' if oil else '금÷은'} {fmt(oil, entry)} → {fmt(oil, cur)} ({mvs}) · 송금까지 {gap_u} {gap_d}".rstrip())
        if rng:
            lines.append(f"   24h 범위 {fmt(oil, rng[0])} ~ {fmt(oil, rng[1])}")
        if c >= th["rebalance"]:
            mates = [n for n in names if accts[n]["role"] != accts[worst]["role"]]
            need, gain = max(0.0, -st[worst]["pnl"]) * 0.5, sum(max(0.0, st[m]["pnl"]) for m in mates) or 1
            todo += [f"{m} → {worst} ${need * max(0.0, st[m]['pnl']) / gain:,.0f}" for m in mates]
        elif c >= 0.25:
            todo.append(f"{worst} 곧 송금 대비 (여유 소진 {c:.0%})")
    lines += ["", "🧯 계정별 여유 소진 (35% 송금 · 55% 지금 송금)"]
    order = sorted(st, key=lambda n: (accts[n]["pair"], accts[n]["role"]))
    row = []
    for n in order:
        c = max(0.0, st[n]["consumed"])
        row.append(f"{'🟠' if c >= th['rebalance'] else '🟡' if c >= 0.25 else '✅'}{n} {c:.0%}")
    for i in range(0, len(row), 3):
        lines.append("   " + " · ".join(row[i:i + 3]))
    lines += ["", "📝 할 일: " + (" / ".join(todo) if todo else "없음"),
              f"CL {px['CL-PERP']:.2f} · BZ {px['BZ-PERP']:.2f} · 금 {px['XAU-PERP']:,.1f} · 은 {px['XAG-PERP']:.3f} ({'Variational' if src == 'variational' else src})"]
    return "\n".join(lines)


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
    for k in [k for k in state if k not in st and not k.startswith("_")]:
        del state[k]
        changed = True
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if datetime.now(KST).hour >= cfg.get("daily_summary_hour_kst", 9) and state.get("_daily") != today and st:
        outbox.append(daily_report(accts, by, st, px, src, th))
        state["_daily"] = today
        changed = True
    if outbox:
        telegram("\n\n".join(outbox))
    if changed:
        save_json("alert_state.json", state)
    print({n: f"소진 {s['consumed']:.0%} 여유 {s['room']:.1%}" for n, s in st.items()})


if __name__ == "__main__":
    main()
