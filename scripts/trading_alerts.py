"""5분마다: 현구 트레이딩 계정(data/trading.json) 손절·바닥선·차값 여유를 확인해 텔레그램 알림.

단계 (높을수록 급함)
  🔴 자산이 바닥선 아래 / 비상 손절가 넘음 / 봉 마감 손절선 아래에서 마감(추정)
  🟠 비상 손절까지 3% 이내 / 봉 마감 손절선 아래로 내려옴 (마감 전)
  🟡 손절 다 맞으면 차값 여유가 갭 여유분 미만 / 손절 미정 / 12/10 점검일
같은 알림은 repeat_minutes(기본 60분)마다 다시, 풀리면 '해제' 한 번. 매일 9시(KST) 이후 첫 실행에 요약 1회.

봉 마감 판정: Variational은 캔들을 안 줘서, 마감 시각(4시간봉 KST 01·05·09·13·17·21시, 크립토 일봉 09시,
주식 일봉은 close_utc_hour — 미국장 21시 UTC = 06시 KST, 평일만) 직후
첫 실행의 mark 가격으로 추정해요. 실제 손절 전에 차트에서 확인하라고 안내해요.
"""
import time
from datetime import datetime, timedelta, timezone

import common
from common import _post, load_json, save_json, telegram, variational_marks, xyz_mids

KST = timezone(timedelta(hours=9))
RANK = {"ok": 0, "am": 1, "or": 2, "dn": 3}
ICON = {"am": "🟡", "or": "🟠", "dn": "🔴"}
usd = lambda v: f"{'−' if v < 0 else ''}${abs(v):,.0f}"
usdS = lambda v: f"{'−' if v < 0 else '+'}${abs(v):,.0f}"


def prc(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}" if v >= 10 else f"{v:.4f}"


def last_close(p, now):
    """가장 최근 봉 마감 시각과 다음 마감 시각 (UTC).
    4시간봉: UTC 0·4·8·12·16·20시 / 크립토 일봉: UTC 0시 /
    주식 일봉(close_utc_hour 지정, 예: 미국장 21시 UTC = 06시 KST): 평일(월~금) 그 시각만."""
    tf = p.get("close_tf", "4시간봉")
    base = now.replace(minute=0, second=0, microsecond=0)
    if tf.startswith("4"):
        lc = base.replace(hour=(now.hour // 4) * 4)
        return lc, lc + timedelta(hours=4)
    h = p.get("close_utc_hour", 0)
    lc = base.replace(hour=h)
    if lc > now:
        lc -= timedelta(days=1)
    if "close_utc_hour" in p:   # 주식: 주말 마감 없음
        while lc.weekday() >= 5:
            lc -= timedelta(days=1)
        nx = lc + timedelta(days=1)
        while nx.weekday() >= 5:
            nx += timedelta(days=1)
        return lc, nx
    return lc, lc + timedelta(days=1)


def evaluate(T, px, now):
    R = T["rules"]
    cash = T["snapshot"]["balance"] - T["snapshot"]["upnl"]
    near = R.get("stop_near_pct", 0.03)
    rows, alerts = [], {}   # alerts: key -> (level, text)
    for p in T["positions"]:
        s = 1 if p["side"] == "long" else -1
        cur = px.get(p["instrument"], p["entry"])
        stop = p.get("stop") if p.get("stop") is not None else p.get("assumed_stop")
        pnl = s * p["qty"] * (cur - p["entry"])
        to_stop = (s * (cur - stop) / cur) if stop is not None else None
        loss = max(0.0, s * p["qty"] * (cur - stop)) if stop is not None else 0.0
        rows.append({"p": p, "cur": cur, "pnl": pnl, "to_stop": to_stop, "loss": loss, "live": p["instrument"] in px})
        sym = p["symbol"]
        if p["instrument"] not in px:
            continue
        if p.get("stop") is not None and to_stop <= 0:
            alerts[f"stop:{sym}"] = ("dn", f"{sym} {prc(cur)} — 비상 손절 {prc(stop)} 넘음 → 정리")
        elif p.get("stop") is not None and to_stop < near:
            alerts[f"stop:{sym}"] = ("or", f"{sym} 손절까지 {to_stop:.1%} ({prc(cur)} → {prc(stop)})")
        cs = p.get("close_stop")
        if cs is not None and s * (cur - cs) < 0:
            tf = p.get("close_tf", "4시간봉")
            lc, nxt = last_close(p, now)
            # 마감 직후 15분 안의 첫 실행이면 '마감 기준 이탈(추정)'
            if now - lc < timedelta(minutes=15):
                alerts[f"close:{sym}"] = ("dn", f"{sym} {tf} 마감 {prc(cur)} — 손절선 {prc(cs)} 아래 마감(추정) → 차트 확인 후 정리")
            else:
                alerts[f"close:{sym}"] = ("or", f"{sym} {prc(cur)} — {prc(cs)} 아래. {tf}이 {nxt.astimezone(KST):%m/%d %H:%M} KST에 이 아래로 마감하면 정리")
        if p.get("stop") is None:
            alerts[f"nostop:{sym}"] = ("am", f"{sym} 손절가 미정 (지금은 {prc(p.get('assumed_stop'))} 가정)")
    equity = cash + sum(r["pnl"] for r in rows)
    worst = equity - sum(r["loss"] for r in rows)
    plan_loss = sum(q.get("max_loss", 0) for q in T.get("plans", []))
    spare = worst - R["reserve_usd"]
    if equity <= R["floor_usd"]:
        alerts["floor"] = ("dn", f"자산 {usd(equity)} — 바닥선 {usd(R['floor_usd'])} 아래 → 남은 포지션 전부 정리")
    if spare < R.get("gap_buffer_usd", 300):
        alerts["spare"] = ("am", f"손절 다 맞으면 차값 여유 {usd(spare)} (갭 여유 ${R.get('gap_buffer_usd', 300)} 미만) → 신규 진입 중지")
    check = datetime.fromisoformat(R["check_date"]).replace(tzinfo=KST)
    if now >= check.astimezone(timezone.utc):
        alerts["check"] = ("am", f"12/10 점검: {R.get('check_note', '')}")
    summary = {"equity": equity, "cash": cash, "worst": worst, "worst_plan": worst - plan_loss, "spare": spare,
               "oi": sum(r["p"]["qty"] * r["cur"] for r in rows), "rows": rows,
               "dday": (check.date() - now.astimezone(KST).date()).days}
    return alerts, summary


def daily(T, S, alerts):
    ts = datetime.now(KST).strftime("%m/%d %H:%M")
    lines = [f"📈 현구 트레이딩 요약 ({ts} KST)", "",
             f"💰 자산 {usd(S['equity'])} · 미실현 {usdS(S['equity'] - S['cash'])} · {S['oi'] / S['equity']:.1f}배",
             f"🧯 손절 다 맞으면 {usd(S['worst'])} → 차값 빼고 여유 {usd(S['spare'])}" + (f" (계획 포함 {usd(S['worst_plan'] - T['rules']['reserve_usd'])})" if T.get("plans") else ""),
             f"📅 12/10 점검 D-{S['dday']}" if S["dday"] > 0 else "📅 12/10 점검일", ""]
    for r in sorted(S["rows"], key=lambda r: r["to_stop"] if r["to_stop"] is not None else 9):
        p = r["p"]
        ts_ = f"손절까지 {r['to_stop']:.1%}" if r["to_stop"] is not None else "손절 없음"
        lines.append(f" {p['symbol']} {prc(r['cur'])} {usdS(r['pnl'])} · {ts_}{'(가정)' if p.get('stop') is None else ''}")
    lines += ["", "📝 할 일: " + (" / ".join(t for _, t in sorted(alerts.values(), key=lambda x: -RANK[x[0]])) if alerts else "없음")]
    return "\n".join(lines)


def hl_name(p):
    """하이퍼리퀴드 이름: 크립토는 기본 덱(BTC), 주식은 xyz 덱(xyz:NVDA). trading.json의 hl로 덮어쓰기 가능."""
    return p.get("hl") or (p["symbol"] if p.get("group") == "크립토" else "xyz:" + p["symbol"])


def get_prices(T, now):
    """Variational mark가 기본, 빠진 종목은 하이퍼리퀴드 중간가. 페이지용으로 data/trading_marks.json에 기록."""
    items = T["positions"] + T.get("plans", [])
    want = {p["instrument"] for p in items}
    px, src, fund = {}, {}, {}
    try:
        vm = variational_marks()
        for k in want:
            if k in vm:
                px[k], src[k] = vm[k], "variational"
        for l in common.LAST_STATS.get("listings", []):
            if l.get("ticker") in want:
                try:
                    fund[l["ticker"]] = float(((l.get("funding") or {}).get("last") or {}).get("annualized_rate") or 0)
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        print("Variational 가격 실패:", e)
    miss = [p for p in items if p["instrument"] not in px]
    if miss:
        mids = {}
        try:
            mids.update({k: float(v) for k, v in _post({"type": "allMids"}).items()})
        except Exception as e:
            print("하이퍼리퀴드 기본 덱 실패:", e)
        try:
            mids.update(xyz_mids())
        except Exception as e:
            print("하이퍼리퀴드 xyz 실패:", e)
        for p in miss:
            if hl_name(p) in mids:
                px[p["instrument"]], src[p["instrument"]] = mids[hl_name(p)], "hyperliquid"
    if px:
        save_json("trading_marks.json", {"time_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "marks": px, "src": src, "funding_apr": fund,
                                          "note": "trading_alerts.py가 5분마다 기록. 페이지는 브라우저에서 Variational을 못 부를 때 이 값을 써요"})
    return px, src


def main():
    T = load_json("trading.json")
    if not T or not T.get("positions"):
        print("trading.json 없음/포지션 없음")
        return
    if not T.get("rules", {}).get("telegram", True):
        print("현구 알림 꺼짐 (rules.telegram=false)")
        return
    now = datetime.now(timezone.utc)
    px, src = get_prices(T, now)
    if not px:
        print("가격 없음")
        return
    alerts, S = evaluate(T, px, now)
    state = load_json("trading_alert_state.json", {})
    repeat = T["rules"].get("repeat_minutes", 60) * 60
    t, out, changed = time.time(), [], False
    for k, (lv, text) in alerts.items():
        prev = state.get(k, {"lv": "ok", "t": 0})
        rep = repeat if lv in ("or", "dn") else 24 * 3600   # 🟡는 하루 한 번만 다시
        if RANK[lv] > RANK.get(prev["lv"], 0) or t - prev["t"] >= rep:
            out.append(f"{ICON[lv]} {text}")
            state[k] = {"lv": lv, "t": t, "text": text}
            changed = True
        elif lv != prev["lv"]:
            state[k] = {"lv": lv, "t": prev["t"], "text": text}
            changed = True
    for k in [k for k in state if not k.startswith("_") and k not in alerts]:
        if state[k]["lv"] in ("dn", "or") and not k.startswith("nostop"):
            out.append(f"✅ 해제 — {state[k].get('text', k)}")
        del state[k]
        changed = True
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if datetime.now(KST).hour >= T["rules"].get("daily_summary_hour_kst", 9) and state.get("_daily") != today:
        out.append(daily(T, S, alerts))
        state["_daily"] = today
        changed = True
    if out:
        telegram("[현구]\n" + "\n".join(out) if len(out) > 1 or not out[0].startswith("📈") else out[0])
    if changed:
        save_json("trading_alert_state.json", state)
    print({"equity": round(S["equity"]), "worst": round(S["worst"]), "alerts": {k: v[0] for k, v in alerts.items()}})


if __name__ == "__main__":
    main()
