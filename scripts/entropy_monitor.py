"""Entropy(하이퍼리퀴드 HIP-3 'io' 덱) DRAM/EWY 페어 OI 모니터.

  python scripts/entropy_monitor.py           # 알림 점검 (10분마다)
  python scripts/entropy_monitor.py --record  # 30분마다 기록 (data/entropy_history.json)

지갑 주소는 Secret ENTROPY_ACCOUNTS = {"A":"0x…","B":"0x…"} 에서만 읽고, 저장 파일엔 라벨만 남겨요.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import _post, load_json, save_json, telegram  # noqa: E402

KST = timezone(timedelta(hours=9))
ICON = {0: "✅", 1: "🟡", 2: "🔴", 3: "🚨"}
NAME = {0: "정상", 1: "주의", 2: "위험", 3: "긴급"}


def usd(x):
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


# ---------------------------------------------------------------- 데이터 받기
def fetch_market(dex):
    """{'DRAM': {mark, oracle, funding_h, oi_usd, vol24}, ...}"""
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": dex})
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        coin = u["name"].split(":")[-1]
        mark = float(c.get("markPx") or 0)
        out[coin] = {
            "mark": mark,
            "oracle": float(c.get("oraclePx") or 0),
            "funding_h": float(c.get("funding") or 0),
            "oi_usd": float(c.get("openInterest") or 0) * mark,
            "vol24": float(c.get("dayNtlVlm") or 0),
            "max_lev": u.get("maxLeverage"),
        }
    return out


def fetch_account(addr, dex):
    return _post({"type": "clearinghouseState", "user": addr, "dex": dex})


def fetch_triggers(addr, dex):
    """걸려 있는 SL/TP(트리거 주문) 목록. 실패하면 None (알림 생략)."""
    try:
        rows = _post({"type": "frontendOpenOrders", "user": addr, "dex": dex})
    except Exception as e:
        print("frontendOpenOrders 실패:", e)
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for o in rows:
        if not isinstance(o, dict) or not o.get("isTrigger"):
            continue
        try:
            out.append({"coin": o.get("coin", "").split(":")[-1], "px": float(o.get("triggerPx")),
                        "sz": float(o.get("sz") or 0), "type": o.get("orderType", "")})
        except (TypeError, ValueError):
            pass
    return out


def fetch_funding_24h(addr, coins):
    """최근 24시간 받은(+)/낸(-) 펀딩 합계 (USDC)."""
    start = int((time.time() - 86400) * 1000)
    try:
        rows = _post({"type": "userFunding", "user": addr, "startTime": start})
    except Exception as e:
        print("userFunding 실패:", e)
        return None
    tot = 0.0
    for r in rows or []:
        d = r.get("delta", {})
        if d.get("coin", "").split(":")[-1] in coins and ":" in d.get("coin", ""):
            tot += float(d.get("usdc") or 0)
    return tot


# ---------------------------------------------------------------- 계산
def analyze_account(key, cfg, raw, market, triggers=None):
    acfg = cfg["accounts"][key]
    ms = raw.get("marginSummary", {})
    av = float(ms.get("accountValue") or 0)
    maint = float(raw.get("crossMaintenanceMarginUsed") or 0)
    legs = []
    for ap in raw.get("assetPositions", []):
        p = ap.get("position", {})
        coin = p.get("coin", "").split(":")[-1]
        szi = float(p.get("szi") or 0)
        if szi == 0:
            continue
        mark = market.get(coin, {}).get("mark") or float(p.get("entryPx") or 0)
        liq = p.get("liquidationPx")
        liq = float(liq) if liq not in (None, "") else None
        side = "long" if szi > 0 else "short"
        dist = None
        if liq and mark:
            dist = (mark - liq) / mark if side == "long" else (liq - mark) / mark
        legs.append({
            "coin": coin, "side": side, "szi": szi, "ntl": abs(szi) * mark,
            "entry": float(p.get("entryPx") or 0), "mark": mark, "upnl": float(p.get("unrealizedPnl") or 0),
            "liq": liq, "liq_dist": dist, "lev_type": (p.get("leverage") or {}).get("type", "?"),
            "lev": (p.get("leverage") or {}).get("value"),
            "margin": float(p.get("marginUsed") or 0),
        })
    # 포지션별 SL/TP: 롱이면 현재가 아래 트리거 = SL, 위 = TP (숏은 반대)
    for l in legs:
        l["sl"] = l["tp"] = None
        if triggers is None:
            l["orders_known"] = False
            continue
        l["orders_known"] = True
        below = [t["px"] for t in triggers if t["coin"] == l["coin"] and t["px"] < l["mark"]]
        above = [t["px"] for t in triggers if t["coin"] == l["coin"] and t["px"] > l["mark"]]
        if l["side"] == "long":
            l["sl"], l["tp"] = (max(below) if below else None), (min(above) if above else None)
        else:
            l["sl"], l["tp"] = (min(above) if above else None), (max(below) if below else None)
    gross = sum(l["ntl"] for l in legs)
    all_cross = bool(legs) and all(l["lev_type"] == "cross" for l in legs)
    mean_leg = gross / len(legs) if legs else 0
    spread_buf = (av - maint) / mean_leg if (all_cross and mean_leg) else None
    ntls = [l["ntl"] for l in legs]
    imbalance = abs(ntls[0] - ntls[1]) / (sum(ntls) / 2) if len(ntls) == 2 and sum(ntls) else None
    have = {l["coin"]: l["side"] for l in legs}
    missing = [f"{c} {s}" for c, s in acfg["legs"].items() if have.get(c) != s]
    extra = [f"{c} {s}" for c, s in have.items() if c not in acfg["legs"]]
    return {
        "key": key, "label": acfg["label"], "equity": av, "maint": maint,
        "free": float(raw.get("withdrawable") or 0),
        "margin_ratio": maint / av if av else None, "gross": gross,
        "lev_gross": gross / av if av else None, "upnl": sum(l["upnl"] for l in legs),
        "spread_buf": spread_buf, "all_cross": all_cross, "imbalance": imbalance,
        "missing": missing, "extra": extra, "legs": legs,
    }


def analyze(cfg, market, accts):
    """accts = [analyze_account 결과...] → (경보 목록, 요약)"""
    al = []  # (key, level, text)

    def lv(x, warn, danger, low_is_bad=True):
        if x is None:
            return 0
        if low_is_bad:
            return 2 if x < danger else 1 if x < warn else 0
        return 2 if x > danger else 1 if x > warn else 0

    for a in accts:
        tag = f"{a['label']}({a['key']})"
        # 1) 포지션 소실/뒤집힘 → OI 사라짐
        if a["missing"]:
            al.append((f"{a['key']}:legs", 3,
                       f"{tag} 포지션 없음/방향 다름: {', '.join(a['missing'])}\n→ 청산·체결됐는지 확인. 반대 계정과 순노출이 생겼을 수 있어요"))
        else:
            al.append((f"{a['key']}:legs", 0, f"{tag} 포지션 정상 복구"))
        # 2) 청산가 거리 — 격리 포지션은 포지션마다 따로 청산되므로 포지션별로 알림.
        #    교차면 포지션별 청산가는 '다른 포지션 고정' 가정이라 헛알림이 나서 스프레드 여유(3번)로만 판단
        if not a["all_cross"]:
            for l in a["legs"]:
                d = l["liq_dist"]
                if d is None:
                    continue
                k = 3 if d <= 0.03 else lv(d, cfg["liq_near"], cfg["liq_imminent"])
                al.append((f"{a['key']}:{l['coin']}:liq", k,
                           f"{tag} {l['coin']} {'롱' if l['side'] == 'long' else '숏'} 청산가 {l['liq']:,.3f}까지 {d:.1%} (현재 {l['mark']:,.3f})"))
        # 3) 교차증거금 스프레드 여유
        if a["spread_buf"] is not None:
            k = lv(a["spread_buf"], cfg["spread_buffer_warn"], cfg["spread_buffer_danger"])
            al.append((f"{a['key']}:spread", k,
                       f"{tag} DRAM-EWY 스프레드가 {a['spread_buf']:.1%} 더 벌어지면 청산 (증거금 ${a['equity']:,.0f}, 포지션당 ${a['gross']/2:,.0f})"))
        # 4) 유지증거금 비율
        k = lv(a["margin_ratio"], cfg["margin_warn"], cfg["margin_danger"], low_is_bad=False)
        if a["margin_ratio"] is not None and a["all_cross"]:
            al.append((f"{a['key']}:margin", k, f"{tag} 유지증거금 사용률 {a['margin_ratio']:.0%}"))
        # 5) 계정 안 두 포지션 불균형 (가격이 따로 움직이면 1:1이 깨짐)
        if a["imbalance"] is not None:
            k = 1 if a["imbalance"] > cfg["leg_imbalance"] else 0
            ls = ", ".join(f"{l['coin']} ${l['ntl']:,.0f}" for l in a["legs"])
            al.append((f"{a['key']}:imb", k, f"{tag} 포지션 크기 차이 {a['imbalance']:.0%} ({ls}) → 작은 쪽 추가/큰 쪽 축소로 1:1 맞추기"))

    # 6) 두 계정 합친 순노출 (한 포지션 사라지면 바로 튐)
    coins = sorted({l["coin"] for a in accts for l in a["legs"]})
    net = {}
    for c in coins:
        s = sum(l["ntl"] * (1 if l["side"] == "long" else -1) for a in accts for l in a["legs"] if l["coin"] == c)
        g = sum(l["ntl"] for a in accts for l in a["legs"] if l["coin"] == c)
        net[c] = (s, g)
        r = abs(s) / g if g else 0
        k = 2 if r > cfg["net_exposure"] * 2 else 1 if r > cfg["net_exposure"] else 0
        al.append((f"net:{c}", k, f"{c} 계정 합산 순노출 {'롱' if s > 0 else '숏'} ${abs(s):,.0f} (총 ${g:,.0f}의 {r:.0%})"))

    # 7) 리밸런싱
    reb = None
    recycle = []
    if accts and all(a["all_cross"] for a in accts) and len(accts) == 2 and all(a["equity"] for a in accts):
        # 교차: 계정 증거금끼리 맞추기
        hi, lo = sorted(accts, key=lambda a: -a["equity"])
        mean = (hi["equity"] + lo["equity"]) / 2
        gap = (hi["equity"] - lo["equity"]) / mean
        amt = (hi["equity"] - lo["equity"]) / 2
        reb = {"from": hi["label"], "to": lo["label"], "amount": amt, "gap": gap}
        k = 1 if gap > cfg["rebalance_gap"] else 0
        al.append(("rebalance", k, f"증거금 차이 {gap:.0%} → {hi['label']}에서 {lo['label']}로 ${amt:,.0f} 옮기면 균형"))
    else:
        # 격리: 같은 종목 롱/숏 포지션끼리 — 수익 포지션 이익을 빼서 손실 포지션 격리마진에 넣기
        target = cfg.get("liq_target", 0.18)
        for c in coins:
            legs = [(a, l) for a in accts for l in a["legs"] if l["coin"] == c and l["liq_dist"] is not None]
            if len(legs) < 2:
                continue
            (la, ll), (wa, wl) = sorted(legs, key=lambda t: t[1]["liq_dist"])[:1] + sorted(legs, key=lambda t: -t[1]["liq_dist"])[:1]
            if la is wa:
                continue
            need = max(0.0, ll["ntl"] * (target - ll["liq_dist"]))
            avail = max(0.0, wl["upnl"])
            r = {"coin": c, "from": wa["label"], "from_side": wl["side"], "to": la["label"], "to_side": ll["side"],
                 "need": need, "avail": avail, "dist": ll["liq_dist"], "target": target, "free_to": la["free"]}
            recycle.append(r)
            k = 1 if ll["liq_dist"] < cfg["liq_near"] else 0
            ko = lambda s_: "롱" if s_ == "long" else "숏"
            # Entropy는 strict isolated: 마진 '추가'만 되고 '제거'는 안 됨 (포지션을 줄여야 빠짐)
            fee = wl["ntl"] * 2 * cfg.get("taker_fee", 0.00009)
            msg = (f"{c} 마진 보충: {la['label']} {c} {ko(ll['side'])} 청산거리 {ll['liq_dist']:.1%} → {target:.0%} 만들려면 ${need:,.0f} 추가\n"
                   f"① {la['label']} 여유잔고 ${la['free']:,.0f}" + (" (부족하면 다른 지갑에서 입금)" if la["free"] < need else "") +
                   f"에서 {c} {ko(ll['side'])} 마진에 ${need:,.0f} 추가 — 거래 없음, 수수료 0")
            if avail > 50:
                msg += (f"\n② 예비금이 모자라면: {wa['label']} {c} {ko(wl['side'])}(이익 ${avail:,.0f})을 닫고 같은 수량 재진입 → 풀린 이익을 {la['label']}로 전송"
                        f" (수수료 약 ${fee:,.1f}, 재진입 후 {c} SL/TP 다시 맞추기)")
            al.append((f"recycle:{c}", k, msg))

    # 8) SL/TP 점검 — 마진을 넣고 빼면 청산가가 움직여서 SL이 청산가 밖으로 밀릴 수 있음
    gap = cfg.get("sl_gap", 0.02)
    sltp = {}
    for c in coins:
        lg = [(a, l) for a in accts for l in a["legs"] if l["coin"] == c and l.get("orders_known")]
        lo = [(a, l) for a, l in lg if l["side"] == "long" and l["liq"]]
        sh = [(a, l) for a, l in lg if l["side"] == "short" and l["liq"]]
        if not lo or not sh:
            continue
        (la, ll), (sa, sl_) = lo[0], sh[0]
        down = round(ll["liq"] * (1 + gap), 3)   # 롱 SL = 숏 TP
        up = round(sl_["liq"] * (1 - gap), 3)    # 숏 SL = 롱 TP
        sltp[c] = {"up": up, "down": down}
        probs = []
        if ll["sl"] is None or sl_["sl"] is None:
            probs.append("SL 없음")
        else:
            if ll["sl"] <= ll["liq"]:
                probs.append(f"{la['label']} 롱 SL {ll['sl']:,.3f}이 청산가 {ll['liq']:,.3f} 아래 → 청산이 먼저 옴")
            if sl_["sl"] >= sl_["liq"]:
                probs.append(f"{sa['label']} 숏 SL {sl_['sl']:,.3f}이 청산가 {sl_['liq']:,.3f} 위 → 청산이 먼저 옴")
        mis = lambda x, y: x is None or y is None or abs(x - y) / y > 0.002
        if not probs and (mis(ll["tp"], sl_["sl"]) or mis(sl_["tp"], ll["sl"])):
            probs.append("미러 어긋남 (한쪽 SL과 반대쪽 TP 가격이 다름)")
        danger = any("먼저" in p for p in probs)
        msg = (f"{c} SL/TP 점검: " + "; ".join(probs) +
               f"\n권장 → 위 {up:,.3f} ({sa['label']} 숏 SL = {la['label']} 롱 TP) · 아래 {down:,.3f} ({la['label']} 롱 SL = {sa['label']} 숏 TP)")
        al.append((f"sltp:{c}", (2 if danger else 1) if probs else 0, msg if probs else f"{c} SL/TP 정상"))

    summary = {"net": net, "rebalance": reb, "recycle": recycle, "sltp": sltp,
               "total_oi": sum(a["gross"] for a in accts),
               "total_equity": sum(a["equity"] for a in accts),
               "total_upnl": sum(a["upnl"] for a in accts)}
    return al, summary


# ---------------------------------------------------------------- 알림 중복 제거
def dispatch(alerts, state, cfg, now):
    repeat = cfg.get("repeat_minutes", 60) * 60
    out = []
    for key, level, text in alerts:
        prev = state.get(key, {"level": 0, "t": 0})
        send = (level > prev["level"]) or (level > 0 and now - prev["t"] >= repeat) or (level == 0 and prev["level"] > 0)
        if send:
            out.append(f"{ICON[level]} [{NAME[level]}] {text}" if level else f"✅ 정상 복귀 · {text}")
            state[key] = {"level": level, "t": now}
        elif level != prev["level"]:
            state[key] = {"level": level, "t": prev["t"]}
    return out


def daily_summary(market, accts, summary, fund24):
    ts = datetime.now(KST).strftime("%m/%d %H:%M")
    d, e = market.get("DRAM", {}), market.get("EWY", {})
    lines = [f"📊 Entropy 일일 요약 ({ts} KST)",
             f"DRAM {d.get('mark', 0):,.3f} · EWY {e.get('mark', 0):,.2f} · 비율 {d.get('mark', 0)/(e.get('mark') or 1):.4f}",
             f"펀딩(연환산) DRAM {d.get('funding_h', 0)*24*365:+.1%} · EWY {e.get('funding_h', 0)*24*365:+.1%}",
             f"내 Entropy OI ${summary['total_oi']:,.0f} · 증거금 ${summary['total_equity']:,.0f} · 미실현 {usd(summary['total_upnl'])}"]
    for a in accts:
        f = fund24.get(a["key"])
        fs = f" · 펀딩24h {usd(f)}" if f is not None else ""
        sb = f" · 스프레드여유 {a['spread_buf']:.0%}" if a["spread_buf"] is not None else ""
        lines.append(f"- {a['label']}: ${a['equity']:,.0f} · 총노출 {a['lev_gross'] or 0:.1f}x{sb}{fs}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 실행
def load_addresses():
    raw = os.environ.get("ENTROPY_ACCOUNTS", "").strip()
    if not raw:
        print("Secret ENTROPY_ACCOUNTS 가 아직 없어요 → 이번 실행은 건너뜀. {\"A\":\"0x…\",\"B\":\"0x…\"} 형태로 등록하세요.")
        sys.exit(0)
    return json.loads(raw)


def collect(cfg):
    addrs = load_addresses()
    market = fetch_market(cfg["dex"])
    accts = []
    for key in cfg["accounts"]:
        if key not in addrs:
            print(f"{key}: 주소 없음 → 건너뜀")
            continue
        accts.append(analyze_account(key, cfg, fetch_account(addrs[key], cfg["dex"]), market,
                                     fetch_triggers(addrs[key], cfg["dex"])))
    return addrs, market, accts


def record(cfg, market, accts):
    hist = load_json("entropy_history.json", []) or []
    row = {"t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")}
    for c in ("DRAM", "EWY"):
        m = market.get(c, {})
        row[c] = {"mark": m.get("mark"), "oracle": m.get("oracle"), "f": m.get("funding_h"), "oi": round(m.get("oi_usd", 0))}
    row["acct"] = {a["label"]: {"eq": round(a["equity"], 2), "g": round(a["gross"]), "u": round(a["upnl"], 2),
                                "sb": None if a["spread_buf"] is None else round(a["spread_buf"], 4)} for a in accts}
    hist.append(row)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg.get("history_days", 30))).strftime("%Y-%m-%dT%H:%M:00Z")
    hist = [h for h in hist if h["t"] >= cutoff]
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "entropy_history.json"),
              "w", encoding="utf-8") as fh:
        json.dump(hist, fh, ensure_ascii=False, separators=(",", ":"))
    print("recorded", row)


def main():
    cfg = load_json("entropy_config.json")
    addrs, market, accts = collect(cfg)
    if "--record" in sys.argv:
        record(cfg, market, accts)
        return
    state = load_json("entropy_alert_state.json", {}) or {}
    alerts, summary = analyze(cfg, market, accts)
    now = time.time()
    out = dispatch(alerts, state, cfg, now)
    # 하루 한 번 요약
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if datetime.now(KST).hour >= cfg.get("daily_summary_hour_kst", 9) and state.get("_daily") != today:
        fund24 = {a["key"]: fetch_funding_24h(addrs[a["key"]], {"DRAM", "EWY"}) for a in accts}
        out.append(daily_summary(market, accts, summary, fund24))
        state["_daily"] = today
    if out:
        telegram("\n\n".join(out))
    save_json("entropy_alert_state.json", state)
    for a in accts:
        print(a["label"], f"eq={a['equity']:.0f} gross={a['gross']:.0f} buf={a['spread_buf']} mr={a['margin_ratio']}")


if __name__ == "__main__":
    main()
