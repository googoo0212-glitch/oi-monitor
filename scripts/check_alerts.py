"""5분마다: 계정 내 페어(CL/BZ, 금/은)의 '청산 여유 소진율'을 계산해 텔레그램 알림.

이 구조는 SL/TP 없이 운영해요. 위험은 두 다리의 가격 '차이'(BZ−CL, 금÷은)뿐이라서
계정별로 '청산까지 남은 가격차($)'로 단계를 나눠요 (잔고·펀딩·송금이 바뀌어도 그대로 맞는 기준).
  $3.5 이하 곧 송금 / $2.7 이하 송금 알림 / $1.6 이하 지금 송금 / $0.8 이하 비상
가격은 Variational mark price가 기본 (청산도 이 가격 기준). 실패하면 하이퍼리퀴드 xyz 중간가로 대체.
실행할 때마다 data/pair_marks.csv 에 가격을 한 줄씩 남겨서 페이지에서 가격차 추이를 그려요.
매일 아침 9시(KST) 이후 첫 실행에서 일일 요약을 한 번 보내요 (Entropy 요약과 같은 형식).
"""
import csv
import os
import time
from datetime import datetime, timedelta, timezone

from common import DATA, load_json, save_json, telegram, telegram_commands, telegram_set_commands, variational_marks, xyz_mids

usd2 = lambda v: f"{'−' if v < 0 else ''}${abs(v):.2f}"
money = lambda v: f"{'−' if v < 0 else '+'}${abs(v):,.0f}"

KST = timezone(timedelta(hours=9))
SYM = {"CL-PERP": "xyz:CL", "BZ-PERP": "xyz:BRENTOIL", "XAU-PERP": "xyz:GOLD", "XAG-PERP": "xyz:SILVER"}  # 기록(csv) 열 순서
EXTRA = {"GOOGL-PERP": "xyz:GOOGL"}   # 페어가 아닌 단독 포지션 (손익만 반영, 기록 안 함)
ALL = {**SYM, **EXTRA}
LEVELS = ["정상", "리밸런싱", "즉시 송금", "비상"]   # 표시는 '송금' (짝의 이긴 계정 → 진 계정)
ICON = {"정상": "✅", "리밸런싱": "🟡", "즉시 송금": "🔴", "비상": "🚨"}
LABEL = {"정상": "정상", "리밸런싱": "송금 필요", "즉시 송금": "지금 송금", "비상": "비상"}


REF_ROOM = 5.4   # 기준 여유: 청산까지 가격차 $5.4 를 '소진 0%'로 봄 → 50%=$2.7, 70%=$1.6, 85%=$0.8 남음


def account_state(name, acct, legs, px):
    """계정 상태. 원유 페어는 '청산까지 남은 가격차($)'가 기준 — 송금·펀딩으로 잔고가 바뀌어도 그대로 맞음."""
    pnl = sum((1 if l["side"] == "long" else -1) * l["qty"] * (px[l["instrument"]] - l["entry"]) for l in legs)
    oi_now = sum(l["qty"] * px[l["instrument"]] for l in legs)
    oi_0 = sum(l["qty"] * l["entry"] for l in legs)
    mm_now, mm_0 = acct["mm_rate"] * oi_now, acct["mm_rate"] * oi_0
    equity = acct["cash"] + acct.get("fund", 0.0) + pnl      # fund = 누적 펀딩 (받으면 +, 내면 −)
    buf0, buf = acct["cash"] - mm_0, equity - mm_now
    leg = max(l["qty"] * px[l["instrument"]] for l in legs)
    q = next((l["qty"] for l in legs if l["instrument"] == "CL-PERP"), None)
    if q:
        room_usd = buf / q                                   # 가격차가 이만큼 불리하게 움직이면 청산
        consumed = 1 - room_usd / REF_ROOM
    else:
        room_usd = None
        consumed = 1 - buf / buf0 if buf0 > 0 else 1
    return {"pnl": pnl, "equity": equity, "mm_usage": mm_now / equity if equity > 0 else 9, "q": q or 1.0,
            "consumed": consumed, "room": buf / leg, "room_usd": room_usd, "buf": buf, "buf0": buf0, "mm": mm_now}


def mates_of(name, accts, st):
    """같은 짝의 반대 방향 계정들 (이긴 쪽) — 송금을 보내는 쪽."""
    a = accts[name]
    return [n for n, x in accts.items() if x["pair"] == a["pair"] and x["role"] != a["role"] and n in st]


def transfer_text(name, s, accts, st):
    """예비금이 없으므로 짝 계정(이긴 쪽)에서 진 쪽으로 — 짝 전체의 청산 여유가 같아지는 금액."""
    ms = mates_of(name, accts, st)
    if not ms:
        return f"{name}에 ${max(0, int(round((REF_ROOM * 0.8 * s['q'] - s['buf']) / 10) * 10)):,} 입금"
    group = [s] + [st[m] for m in ms]
    star = sum(g["buf"] for g in group) / sum(g["q"] for g in group)      # 맞춘 뒤 각 계정의 여유($ 가격차)
    out = []
    for m in ms:
        give = st[m]["buf"] - star * st[m]["q"]
        if give >= 5:
            out.append(f"{m} → {name} ${int(round(give / 10) * 10):,} 송금")
    return " / ".join(out) + f" (옮기면 양쪽 모두 청산까지 ${star:.1f})" if out else f"{name}: 짝 계정에도 여유가 없어요 — 두 계정 포지션을 같이 줄이기"


def pair_view(legs, acct, px):
    """가격차(원유 BZ−CL) 또는 비율(금÷은): 진입값, 지금, 소진 f가 되는 값(f=1이면 청산), 손해 방향."""
    by = {l["instrument"]: l for l in legs}
    mm0 = acct["mm_rate"] * sum(l["qty"] * l["entry"] for l in legs)
    buf0 = acct["cash"] - mm0
    if "CL-PERP" in by:
        e, c = by["BZ-PERP"]["entry"] - by["CL-PERP"]["entry"], px["BZ-PERP"] - px["CL-PERP"]
        q, up = by["CL-PERP"]["qty"], by["CL-PERP"]["side"] == "long"
        p2 = {l["instrument"]: px.get(l["instrument"], l["entry"]) for l in legs}
        room = account_state("", acct, legs, p2)["room_usd"]
        # 여유가 (1−f)×기준 만큼 남는 가격차. f=0.5 → 송금선, f=1 → 청산
        return {"oil": True, "entry": e, "cur": c, "up": up, "room": room,
                "at": lambda f: c + (room - (1 - f) * REF_ROOM) if up else c - (room - (1 - f) * REF_ROOM)}
    e, c = by["XAU-PERP"]["entry"] / by["XAG-PERP"]["entry"], px["XAU-PERP"] / px["XAG-PERP"]
    unit, up = e * buf0 / (by["XAU-PERP"]["qty"] * by["XAU-PERP"]["entry"]), by["XAU-PERP"]["side"] == "short"
    return {"oil": False, "entry": e, "cur": c, "up": up, "room": None, "at": lambda f: e + f * unit if up else e - f * unit}


def funding_accrual(accts, by, start_utc):
    """funding.csv(30분 기록)로 계정별 누적 펀딩을 추정. {계정: $} (받으면 +).
    계정에 fund_anchor(실제 화면에서 확인한 누적값과 시각)가 있으면 거기서부터 이어서 계산."""
    path = os.path.join(DATA, "funding.csv")
    if not os.path.exists(path):
        return {}
    P = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
    ser = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("perp_last_apr") not in (None, "") and r.get("mark_price"):
                ser.setdefault(r["ticker"], []).append((P(r["time_utc"]), float(r["perp_last_apr"]), float(r["mark_price"])))
    now = datetime.now(timezone.utc)

    def per_unit_long(tk, t0):
        s = ser.get(tk, [])
        tot = 0.0
        for i, (t, apr, price) in enumerate(s):
            nxt = s[i + 1][0] if i + 1 < len(s) else min(now, t + timedelta(hours=8))
            a, b = max(t, t0), min(nxt, now)
            if b > a:
                tot += -apr * price * (b - a).total_seconds() / 31557600   # 음수 펀딩이면 롱이 받음
        return tot

    cache, out = {}, {}
    for name, legs in by.items():
        a = accts.get(name, {})
        anchor = a.get("fund_anchor") or {}
        t0 = P(anchor["time_utc"]) if anchor.get("time_utc") else start_utc
        f = anchor.get("usd", 0.0)
        for l in legs:
            k = (l["instrument"], t0)
            if k not in cache:
                cache[k] = per_unit_long(l["instrument"], t0)
            f += (1 if l["side"] == "long" else -1) * l["qty"] * cache[k]
        out[name] = round(f, 2)
    return out


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


def daily_report(accts, by, st, px, src, th, title="일일 요약"):
    """계정별 '잔고 → 청산선 (여유)' 중심 리포트."""
    ts = datetime.now(KST).strftime("%m/%d %H:%M")
    oi = sum(l["qty"] * px.get(l["instrument"], l["entry"]) for legs in by.values() for l in legs)
    eq = sum(s["equity"] for s in st.values())
    base = sum(a.get("start_equity", 0) + a.get("deposits", 0) for n, a in accts.items() if n in st)
    since = load_json("positions.json", {}).get("start", {}).get("time", "")[5:10].replace("-", "/")
    sp = px["BZ-PERP"] - px["CL-PERP"]
    rng = day_range("CL", "BZ", True)
    lines = [f"📊 Variational {title} ({ts} KST)", "",
             f"🧱 OI ${oi:,.0f} · 투입 자본 ${eq:,.0f}",
             f"📈 {since} 시작 대비 {money(eq - base)} (입금 제외)",
             f"📏 가격차 BZ−CL ${sp:.2f} (괴리율 {sp / px['CL-PERP']:.1%})" + (f" · 24h ${rng[0]:.2f}~{rng[1]:.2f}" if rng else ""),
             "💸 펀딩 누적: A 계정 {} · B 계정 {} (짝끼리 상쇄, 잔고에 반영)".format(
                 money(sum(a.get("fund", 0) for n, a in accts.items() if n in st and a["role"] == "A")),
                 money(sum(a.get("fund", 0) for n, a in accts.items() if n in st and a["role"] == "B"))),
             "", "💰 잔고 → 청산선 · 청산까지 남은 가격차 · 송금선(이 가격차가 되면 짝 계정에서 송금)"]
    icon = lambda c: "🔴" if c >= th["urgent"] else "🟠" if c >= th["rebalance"] else "🟡" if c >= th["pre"] else "✅"
    todo = []
    for pid in sorted({a["pair"] for a in accts.values()}):
        names = sorted([n for n, a in accts.items() if a["pair"] == pid and n in st], key=lambda n: accts[n]["role"])
        if not names:
            continue
        lines.append(load_json("positions.json", {}).get("pairs", {}).get(str(pid), f"짝{pid}"))
        for n in names:
            s, v = st[n], pair_view(by[n], accts[n], px)
            c = s["consumed"]
            lines.append(f" {icon(c)} {n} ${s['equity']:,.0f} → ${s['mm']:,.0f} · 청산까지 ${s['room_usd']:.1f} · 송금선 {usd2(v['at'](th['rebalance']))}")
            if c >= th["rebalance"]:
                todo.append(transfer_text(n, s, accts, st))
            elif c >= th["pre"]:
                todo.append(f"{n} 곧 송금 필요 (가격차 ${v['at'](th['rebalance']):.2f}에서)")
    lines += ["", "📝 할 일: " + (" / ".join(todo) if todo else "없음"),
              f"CL {px['CL-PERP']:.2f} · BZ {px['BZ-PERP']:.2f}" + (f" · GOOGL {px['GOOGL-PERP']:.2f}" if 'GOOGL-PERP' in px else "") + f" ({'Variational' if src == 'variational' else src})"]
    return "\n".join(lines)


def dump_listings():
    """Variational 전체 종목 목록(증거금·OI·펀딩 등 원본)을 12시간에 한 번 data/var_listings.json 에 저장 — 종목 비교용."""
    import common
    path = os.path.join(DATA, "var_listings.json")
    try:
        if not common.LAST_STATS:
            return
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600 and os.path.getsize(path) > 1000:
            return
        import json as _json
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump({"saved_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"), **common.LAST_STATS},
                       fh, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        print("종목 목록 저장 실패:", e)


def backup_funding():
    """펀딩 기록 워크플로가 밀려서 funding.csv가 90분 넘게 멈춰 있으면 여기서 대신 한 번 기록."""
    path = os.path.join(DATA, "funding.csv")
    try:
        last = ""
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                last = line
        t = datetime.fromisoformat(last.split(",")[0].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - t < timedelta(minutes=90):
            return
        import record_funding
        record_funding.main()
        print("펀딩 기록 대신 실행 (마지막 기록", t, ")")
    except (Exception, SystemExit) as e:
        print("펀딩 대신 기록 실패:", e)


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
    th = {"rebalance": 0.5, "urgent": 0.7, "emergency": 0.85, "pre": 0.35, **cfg.get("pair_levels", {})}
    data = load_json("positions.json", {})
    accts, pos = data.get("accounts", {}), data.get("positions", [])
    if not pos:
        print("포지션 없음")
        return
    px, src, vm_keys, state_err = {}, "variational", set(), "not tried"
    try:
        vm = variational_marks()
        state_err = None
        vm_keys = {k for k in ALL if k in vm}
        px = {k: vm[k] for k in ALL if k in vm}
    except Exception as e:
        print("Variational 가격 실패:", e)
        state_err = f"{type(e).__name__}: {e}"[:200]
    if len(px) < len(ALL):
        try:
            mids = xyz_mids()
            for k, v in ALL.items():
                if k not in px and v in mids:
                    px[k] = mids[v]
        except Exception as e:
            print("하이퍼리퀴드 가격 실패:", e)
    if src == "variational" and any(k not in vm_keys for k in SYM if k in px):
        src = "hyperliquid" if not any(k in vm_keys for k in SYM) else "mixed"
    log_marks(px, src)
    dump_listings()
    backup_funding()
    state = load_json("alert_state.json", {})
    now, changed, outbox = time.time(), False, []
    if state.get("_var_err") != state_err:
        state["_var_err"] = state_err
        changed = True
    by = {}
    for p in pos:
        by.setdefault(p["account"], []).append(p)
    # 누적 펀딩 반영 (짝끼리는 상쇄되지만 계정별 잔고는 달라져요) + 페이지용 파일 저장
    try:
        t = data.get("start", {}).get("time", "")[:16]
        start_utc = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=KST).astimezone(timezone.utc)
        fund = funding_accrual(accts, by, start_utc)
        for n, f in fund.items():
            if n in accts:
                accts[n] = dict(accts[n], fund=f)
        save_json("funding_accrual.json", {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"),
                                           "since": data.get("start", {}).get("time"), "accounts": fund,
                                           "note": "계정별 누적 펀딩 추정 ($, 받으면 +). funding.csv 기반, fund_anchor가 있으면 실제값에서 이어 계산"})
    except Exception as e:
        print("펀딩 누적 계산 실패:", e)
    st = {}
    for name, legs in by.items():
        if name not in accts or any(l["instrument"] not in px for l in legs if l["instrument"] in SYM):
            continue
        p2 = dict(px, **{l["instrument"]: l["entry"] for l in legs if l["instrument"] not in px})  # 단독 종목 가격 없으면 진입가
        st[name] = account_state(name, accts[name], legs, p2)
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
            if rank == 0:
                msg = f"✅ {name} 정상 복귀 · {describe(by[name], px)} · 청산까지 가격차 ${s['room_usd']:.1f}"
            else:
                tx = transfer_text(name, s, accts, st)
                tail = {"리밸런싱": f"\n→ {tx}",
                        "즉시 송금": f"\n→ 지금 {tx}",
                        "비상": f"\n→ 지금 {tx} · 어려우면 {name}의 두 다리를 같은 수량으로 같이 줄이기"}[level]
                msg = (f"{ICON[level]} [{LABEL[level]}] {name} ({ts} KST)\n"
                       f"{describe(by[name], px)} · 청산까지 가격차 ${s['room_usd']:.2f}"
                       f" · 잔고 ${s['equity']:,.0f} (청산선 ${s['mm']:,.0f}) · 유지증거금 사용률 약 {s['mm_usage']:.0%}{tail}")
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
    # 텔레그램 /report → 지금 상황 리포트 (다음 실행 때 응답)
    if not state.get("_cmds"):
        try:
            state["_cmds"] = bool(telegram_set_commands([("report", "지금 Variational 상황 리포트")]))
            changed = True
        except Exception as e:
            print("명령 메뉴 등록 실패:", e)
    try:
        cmds, off = telegram_commands(state.get("_tg_offset", 0))
        if off != state.get("_tg_offset", 0):
            state["_tg_offset"] = off
            changed = True
        if "/report" in cmds and st:
            outbox.append(daily_report(accts, by, st, px, src, th, title="현황 리포트 (요청)"))
    except Exception as e:
        print("텔레그램 명령 확인 실패:", e)
    if outbox:
        telegram("\n\n".join(outbox))
    if changed:
        save_json("alert_state.json", state)
    print({n: f"청산까지 ${s['room_usd']:.2f}" if s["room_usd"] is not None else f"소진 {s['consumed']:.0%}" for n, s in st.items()})


if __name__ == "__main__":
    main()
