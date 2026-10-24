import os, time, datetime as dt
import requests
import scanner

scanner.get_funding = lambda inst: 0.0  # backtest mein funding ignore

try:
    DAYS = max(1, min(int(os.getenv("DAYS", "1")), 30))
except ValueError:
    DAYS = 1
MAX_HOLD = 48   # 48 x 15m = 12 ghante
COOLDOWN = 4    # same coin+side ke beech 1 ghanta gap
IST = scanner.IST


def fetch_history(inst, bar, count):
    seen, after = {}, ""
    while len(seen) < count + 20:
        params = {"instId": inst, "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        r = requests.get(f"{scanner.OKX}/api/v5/market/history-candles", params=params, timeout=20)
        r.raise_for_status()
        rows = r.json()["data"]
        if not rows:
            break
        for x in rows:
            if x[8] == "1":
                seen[int(x[0])] = x
        after = rows[-1][0]
        time.sleep(0.15)
    rows = [seen[k] for k in sorted(seen)][-count:]
    return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4]), "v": float(x[5])} for x in rows]


def simulate(s, m15, i):
    side, e, sl, tp1, tp2 = s["side"], s["entry"], s["sl"], s["tp1"], s["tp2"]
    risk = abs(e - sl)
    sgn = 1 if side == "LONG" else -1
    stop, hit1 = sl, False
    end = min(i + MAX_HOLD, len(m15) - 1)
    for j in range(i + 1, end + 1):
        c = m15[j]
        if side == "LONG":
            stop_hit, t1, t2 = c["l"] <= stop, c["h"] >= tp1, c["h"] >= tp2
        else:
            stop_hit, t1, t2 = c["h"] >= stop, c["l"] <= tp1, c["l"] <= tp2
        if not hit1:
            if stop_hit:
                return "SL", -1.0
            if t1:
                hit1, stop = True, e
                if t2:
                    return "TP2", 2.5
        else:
            if stop_hit:
                return "TP1+BE", 1.0
            if t2:
                return "TP2", 2.5
    open_r = sgn * (m15[end]["c"] - e) / risk
    r = 1.0 + 0.5 * open_r if hit1 else open_r
    return ("OPEN" if i + MAX_HOLD >= len(m15) - 1 else "TIMEOUT"), r


def run_coin(coin):
    inst = f"{coin}-USDT-SWAP"
    m15 = fetch_history(inst, "15m", DAYS * 96 + 320)
    h1 = fetch_history(inst, "1H", DAYS * 24 + 260)
    res, last_idx = [], {}
    start = max(60, len(m15) - DAYS * 96)
    for i in range(start, len(m15) - 1):
        m = m15[max(0, i - 299):i + 1]
        t_end = m[-1]["t"] + 15 * 60 * 1000
        h = [x for x in h1 if x["t"] + 3600000 <= t_end][-300:]
        if len(m) < 60 or len(h) < 210:
            continue
        sigs = [s for s in (f(m, h) for f in scanner.STRATS) if s]
        for side in ("LONG", "SHORT"):
            group = [s for s in sigs if s["side"] == side]
            if not group or i - last_idx.get((coin, side), -999) < COOLDOWN:
                continue
            setup = scanner.build(coin, m, h, group)
            if not setup:
                continue
            last_idx[(coin, side)] = i
            outcome, r = simulate(setup, m15, i)
            when = dt.datetime.fromtimestamp((m[-1]["t"] + 900000) / 1000, IST)
            res.append(dict(time=when, coin=coin, side=side, strat=" + ".join(setup["strategies"]),
                            outcome=outcome, r=r))
    return res


def send_long(lines):
    chunk = ""
    for ln in lines:
        if len(chunk) + len(ln) > 3800:
            scanner.telegram(chunk)
            chunk = ""
        chunk += ln + "\n"
    if chunk:
        scanner.telegram(chunk)


def summary(rs):
    n = len(rs)
    wins = sum(1 for x in rs if x["r"] > 0)
    tot = sum(x["r"] for x in rs)
    return f"{n} setups | win {wins}/{n} ({100 * wins / n:.0f}%) | total {tot:+.1f}R | avg {tot / n:+.2f}R"


def main():
    allr = []
    for coin in scanner.COINS:
        try:
            allr += run_coin(coin)
        except Exception as ex:
            print(coin, "error:", ex)
    allr.sort(key=lambda x: x["time"])
    head = [f"📊 Backtest: last {DAYS} din | 15m | top 10 coins",
            "Rule: TP1 pe 50% exit + SL entry pe, TP2 baaki. Fees/slippage ignore. Win = R>0", ""]
    if not allr:
        send_long(head + ["Is period mein koi setup nahi bana."])
        return
    head.append("TOTAL: " + summary(allr))
    for name in sorted({x["strat"] for x in allr}):
        head.append(f"• {name}: " + summary([x for x in allr if x["strat"] == name]))
    head += ["", "Setups (IST):"]
    shown = allr[-40:]
    if len(allr) > 40:
        head.append(f"(pehle ke {len(allr) - 40} setups chhupaye)")
    for x in shown:
        head.append(f"{x['time']:%d %b %H:%M} {x['coin']} {x['side']} | {x['strat']} | {x['outcome']} {x['r']:+.1f}R")
    send_long(head)


if __name__ == "__main__":
    main()
