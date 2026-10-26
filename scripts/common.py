"""공용 함수: 파일 경로, 텔레그램 전송, 하이퍼리퀴드 가격."""
import json
import os
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load_json(name, default=None):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def telegram(text):
    """TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 Secrets에 있으면 전송, 없으면 출력만."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print("[telegram]", text.replace("\n", " | "))
    if not token or not chat:
        print("  (secrets 없음 → 전송 생략)")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20) as r:
        r.read()


def _post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def xyz_mids():
    """하이퍼리퀴드 xyz 덱 전체 중간가 {'xyz:CL': 101.2, ...}."""
    try:
        return {k: float(v) for k, v in _post({"type": "allMids", "dex": "xyz"}).items()}
    except Exception as e:
        print("allMids 실패:", e)
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    return {u["name"]: float(c.get("midPx") or c["markPx"]) for u, c in zip(meta["universe"], ctxs)}


def hl_prices():
    """{'XAU': 금 가격, 'XAG': 은 가격} — 하이퍼리퀴드 xyz 덱 중간가."""
    want = {"XAU": "xyz:GOLD", "XAG": "xyz:SILVER"}
    try:
        mids = _post({"type": "allMids", "dex": "xyz"})
        out = {k: float(mids[v]) for k, v in want.items() if v in mids}
        if len(out) == 2:
            return out
    except Exception as e:  # 폴백으로 넘어감
        print("allMids 실패:", e)
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    names = [u["name"] for u in meta["universe"]]
    out = {}
    for k, v in want.items():
        if v in names:
            c = ctxs[names.index(v)]
            out[k] = float(c.get("midPx") or c["markPx"])
    return out
