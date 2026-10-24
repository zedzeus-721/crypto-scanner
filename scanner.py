import os, json, datetime as dt
import requests

OKX = "https://www.okx.com"
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "SUI"]
RISK_USD = float(os.getenv("RISK_USD", "1"))   # risk per trade in $
MAX_LEV = int(os.getenv("MAX_LEV", "15"))      # leverage cap
VALID_MIN = 30
COOLDOWN_MIN = 60
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# ---------- data ----------
def get_candles(inst, bar, limit=300):
    r = requests.get(f"{OKX}/api/v5/market/candles",
                     params={"instId": inst, "bar": bar, "limit": limit}, timeout=20)
    r.raise_for_status()
    rows = [x for x in r.json()["data"] if x[8] == "1"]  # closed candles only
    rows.reverse()
    return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4]), "v": float(x[5])} for x in rows]


def get_funding(inst):
    try:
        r = requests.get(f"{OKX}/api/v5/public/funding-rate", params={"instId": inst}, timeout=20)
        return float(r.json()["data"][0]["fundingRate"])
    except Exception:
        return 0.0


# ---------- indicators ----------
def ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    out = []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(vals, n=14):
    g, l = [0], [0]
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        g.append(max(d, 0))
        l.append(max(-d, 0))
    ag, al = sum(g[1:n + 1]) / n, sum(l[1:n + 1]) / n
    out = [50] * len(vals)
    for i in range(n + 1, len(vals)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_last(m, n=14):
    trs = []
    for i in range(len(m) - n, len(m)):
        trs.append(max(m[i]["h"] - m[i]["l"],
                       abs(m[i]["h"] - m[i - 1]["c"]),
                       abs(m[i]["l"] - m[i - 1]["c"])))
    return sum(trs) / n


def trend_h1(h):
    hc = [x["c"] for x in h]
    e50, e200 = ema(hc, 50), ema(hc, 200)
    if hc[-1] > e50[-1] > e200[-1]:
        return "UP"
    if hc[-1] < e50[-1] < e200[-1]:
        return "DOWN"
    return "MIXED"


# ---------- strategies (each returns dict or None) ----------
def s_pullback(m, h):
    c = [x["c"] for x in m]
    e21 = ema(c, 21)[-1]
    r = rsi(c)[-1]
    a = atr_last(m)
    t = trend_h1(h)
    last = m[-1]
    if t == "UP" and last["l"] <= e21 < last["c"] and last["c"] > last["o"] and r > 40:
        return dict(side="LONG", entry=last["c"],
                    sl=min(x["l"] for x in m[-3:]) - 0.2 * a, name="Trend Pullback")
    if t == "DOWN" and last["h"] >= e21 > last["c"] and last["c"] < last["o"] and r < 60:
        return dict(side="SHORT", entry=last["c"],
                    sl=max(x["h"] for x in m[-3:]) + 0.2 * a, name="Trend Pullback")
    return None


def s_sweep(m, h):
    last, prev, a = m[-1], m[-21:-1], atr_last(m)
    lo, hi = min(x["l"] for x in prev), max(x["h"] for x in prev)
    rng = last["h"] - last["l"]
    if rng <= 0:
        return None
    if last["l"] < lo < last["c"] and last["c"] > last["o"] \
            and (min(last["o"], last["c"]) - last["l"]) >= 0.5 * rng:
        return dict(side="LONG", entry=last["c"], sl=last["l"] - 0.1 * a, name="Liquidity Sweep")
    if last["h"] > hi > last["c"] and last["c"] < last["o"] \
            and (last["h"] - max(last["o"], last["c"])) >= 0.5 * rng:
        return dict(side="SHORT", entry=last["c"], sl=last["h"] + 0.1 * a, name="Liquidity Sweep")
    return None


def s_retest(m, h):
    last, a = m[-1], atr_last(m)
    avgv = sum(x["v"] for x in m[-30:-1]) / 29
    for k in range(2, 7):
        b = m[-k]
        base = m[-k - 20:-k]
        hi, lo = max(x["h"] for x in base), min(x["l"] for x in base)
        between = m[-k + 1:-1]
        if b["c"] > hi and b["v"] > 1.5 * avgv and all(x["c"] > hi for x in between) \
                and last["l"] <= hi + 0.2 * a and last["c"] > hi and last["c"] > last["o"]:
            return dict(side="LONG", entry=last["c"],
                        sl=min(last["l"], hi) - 0.3 * a, name="Breakout Retest")
        if b["c"] < lo and b["v"] > 1.5 * avgv and all(x["c"] < lo for x in between) \
                and last["h"] >= lo - 0.2 * a and last["c"] < lo and last["c"] < last["o"]:
            return dict(side="SHORT", entry=last["c"],
                        sl=max(last["h"], lo) + 0.3 * a, name="Breakout Retest")
    return None


STRATS = [s_pullback, s_sweep, s_retest]


# ---------- build setup ----------
def fmt(x):
    return f"{x:.6g}"


def build(coin, m, h, sigs):
    side = sigs[0]["side"]
    entry, sl = sigs[0]["entry"], sigs[0]["sl"]
    risk = abs(entry - sl)
    sl_pct = risk / entry
    if not (0.0015 <= sl_pct <= 0.03):
        return None
    sgn = 1 if side == "LONG" else -1
    tp1, tp2 = entry + sgn * 2 * risk, entry + sgn * 3 * risk
    a = atr_last(m)
    t = trend_h1(h)
    fund = get_funding(f"{coin}-USDT-SWAP")
    avgv = sum(x["v"] for x in m[-21:-1]) / 20

    score = 50
    if (side == "LONG" and t == "UP") or (side == "SHORT" and t == "DOWN"):
        score += 10
    elif t != "MIXED":
        score -= 10
    if m[-1]["v"] > avgv:
        score += 10
    if (side == "LONG" and fund < 0.0005) or (side == "SHORT" and fund > -0.0005):
        score += 10
    score += 10 * (len(sigs) - 1)
    score = max(30, min(90, score))

    lev = max(1, min(MAX_LEV, int(1 / (2 * sl_pct))))
    notional = RISK_USD / sl_pct
    now = dt.datetime.now(dt.timezone.utc)
    return dict(
        coin=coin, side=side, strategies=[s["name"] for s in sigs],
        entry=entry, entry_low=entry - 0.1 * a, entry_high=entry + 0.1 * a,
        sl=sl, tp1=tp1, tp2=tp2, confidence=score, leverage=lev,
        risk_usd=RISK_USD, notional=round(notional, 2), margin=round(notional / lev, 2),
        trend_1h=t, funding=fund, status="active", created_at=now,
        valid_until=now + dt.timedelta(minutes=VALID_MIN), candle_ts=m[-1]["t"],
    )


def message(s):
    ist = s["valid_until"].astimezone(IST).strftime("%H:%M")
    e = "🟢" if s["side"] == "LONG" else "🔴"
    return (
        f"{e} {s['coin']}/USDT {s['side']}  (15m)\n"
        f"Strategy: {' + '.join(s['strategies'])}\n"
        f"Entry zone: {fmt(s['entry_low'])} - {fmt(s['entry_high'])}\n"
        f"SL: {fmt(s['sl'])}\n"
        f"TP1 (1:2): {fmt(s['tp1'])}\n"
        f"TP2 (1:3): {fmt(s['tp2'])}\n"
        f"Leverage: {s['leverage']}x (max safe by SL)\n"
        f"Risk ${s['risk_usd']:g} -> Position ${s['notional']:g}, Margin ~${s['margin']:g}\n"
        f"Score: {s['confidence']}/100 | 1h trend: {s['trend_1h']}\n"
        f"Valid till {ist} IST. Price entry zone se door chala gaya ho to skip karo.\n"
        f"TP1 pe SL entry pe le aana."
    )


# ---------- io ----------
def telegram(text):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    r.raise_for_status()


def get_db():
    key = os.getenv("FIREBASE_KEY")
    if not key:
        return None
    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(json.loads(key)))
    return firestore.client()


def main():
    db = get_db()
    if os.getenv("TEST_MODE") == "1":
        telegram("✅ Scanner connected. Telegram OK.")
        if db:
            db.collection("setups").document("test").set(
                {"test": True, "created_at": dt.datetime.now(dt.timezone.utc)})
            telegram("✅ Firebase OK.")
        return

    recent = set()
    if db:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=COOLDOWN_MIN)
        for d in db.collection("setups").where("created_at", ">=", cutoff).stream():
            x = d.to_dict()
            recent.add((x.get("coin"), x.get("side")))

    found = 0
    for coin in COINS:
        try:
            inst = f"{coin}-USDT-SWAP"
            m, h = get_candles(inst, "15m"), get_candles(inst, "1H")
            if len(m) < 60 or len(h) < 210:
                continue
            sigs = [s for s in (f(m, h) for f in STRATS) if s]
            for side in ("LONG", "SHORT"):
                group = [s for s in sigs if s["side"] == side]
                if not group or (coin, side) in recent:
                    continue
                setup = build(coin, m, h, group)
                if not setup:
                    continue
                telegram(message(setup))
                if db:
                    db.collection("setups").document(f"{coin}-{side}-{setup['candle_ts']}").set(setup)
                found += 1
        except Exception as ex:
            print(f"{coin} error: {ex}")
    print(f"done, setups found: {found}")


if __name__ == "__main__":
    main()
