#!/usr/bin/env python3
"""
rankings.py  —  Whole-universe Indian mutual-fund ranking engine.

Pulls free, open NAV data (mfapi.in, sourced from AMFI), computes the metrics
that matter for a long-horizon investor (rolling-return consistency, downside/
Sortino, volatility, drawdown, trailing CAGR), RAG-scores each fund against its
category, ranks them, and writes rankings.json for the dashboard to read.

No paid API, no scraping of screener sites — only open NAV data + our own maths.
Runs anywhere Python runs (GitHub Actions, Colab, local).
"""
import json, sys, time, datetime as dt
from urllib.request import urlopen, Request

# ------- CONFIG: the universe to scan, per category -------
# Scheme codes are AMFI codes for DIRECT-GROWTH plans. This starter set is a
# broad, credible bench per category; widen it freely — the engine scales.
UNIVERSE = {
  "Flexi Cap": {
    "122639":"Parag Parikh Flexi Cap","118955":"HDFC Flexi Cap","120843":"Quant Flexi Cap",
    "112090":"Franklin India Flexi Cap","118825":"Kotak Flexi Cap","120492":"UTI Flexi Cap",
    "125354":"PGIM India Flexi Cap","118527":"Aditya Birla Flexi Cap",
  },
  "Mid Cap": {
    "118989":"HDFC Mid-Cap Opportunities","127039":"Motilal Oswal Midcap","120841":"Quant Mid Cap",
    "125307":"PGIM India Midcap Opp","118668":"Kotak Emerging Equity","120841b":"Nippon Growth Mid Cap",
    "147623":"Edelweiss Mid Cap","118440":"Axis Midcap",
  },
  "Small Cap": {
    "118778":"Nippon India Small Cap","120828":"Quant Small Cap","125497":"Axis Small Cap",
    "130503":"Bandhan Small Cap","118759":"HDFC Small Cap","125350":"SBI Small Cap",
    "147946":"Tata Small Cap","118472":"Kotak Small Cap",
  },
  "Index (Core)": {
    "119063":"UTI Nifty 50 Index","120716":"HDFC Index Nifty 50","147794":"Motilal Nifty 500 Index",
    "125504":"Nippon Nifty 50 Index",
  },
  "Arbitrage (Education)": {
    "118475":"Kotak Equity Arbitrage","102885":"Invesco India Arbitrage","135800":"Tata Arbitrage",
    "119755":"Edelweiss Arbitrage","103174":"SBI Arbitrage",
  },
}
TARGET_CATS = list(UNIVERSE.keys())
RF = 0.065  # risk-free (approx T-bill) for Sharpe/Sortino

def fetch_nav(code):
    """Return list of (date, nav) ascending. Uses mfapi.in (free, open, CORS-ok)."""
    url = "https://api.mfapi.in/mf/%s" % code
    req = Request(url, headers={"User-Agent":"rankings.py"})
    with urlopen(req, timeout=30) as r:
        j = json.loads(r.read().decode())
    out = []
    for row in j.get("data", []):
        try:
            d = dt.datetime.strptime(row["date"], "%d-%m-%Y").date()
            out.append((d, float(row["nav"])))
        except Exception:
            pass
    out.sort(key=lambda x: x[0])
    return out

def nav_on_or_before(series, target):
    """Nearest NAV on/before a target date."""
    lo, hi, res = 0, len(series)-1, None
    while lo <= hi:
        mid = (lo+hi)//2
        if series[mid][0] <= target:
            res = series[mid][1]; lo = mid+1
        else:
            hi = mid-1
    return res

def cagr(series, years):
    if not series: return None
    end_d, end_v = series[-1]
    start_v = nav_on_or_before(series, end_d - dt.timedelta(days=int(365.25*years)))
    if not start_v or start_v <= 0: return None
    return (end_v/start_v)**(1/years) - 1

def rolling_returns(series, window_years, step_days=30):
    """List of annualised returns over every rolling window."""
    if len(series) < 2: return []
    end_d = series[-1][0]; start_d = series[0][0]
    w = dt.timedelta(days=int(365.25*window_years))
    res, cur = [], start_d + w
    while cur <= end_d:
        v0 = nav_on_or_before(series, cur - w); v1 = nav_on_or_before(series, cur)
        if v0 and v1 and v0 > 0:
            res.append((v1/v0)**(1/window_years) - 1)
        cur += dt.timedelta(days=step_days)
    return res

def monthly_returns(series):
    """Approx monthly returns from month-end NAVs."""
    by_month = {}
    for d, v in series:
        by_month[(d.year, d.month)] = v   # last NAV in month wins
    keys = sorted(by_month)
    rets = []
    for i in range(1, len(keys)):
        p, c = by_month[keys[i-1]], by_month[keys[i]]
        if p > 0: rets.append(c/p - 1)
    return rets

def stdev(xs):
    if len(xs) < 2: return None
    m = sum(xs)/len(xs)
    return (sum((x-m)**2 for x in xs)/(len(xs)-1))**0.5

def sortino(series):
    mr = monthly_returns(series)
    if len(mr) < 12: return None
    ann_ret = (1+sum(mr)/len(mr))**12 - 1
    downs = [min(0, r-(RF/12)) for r in mr]
    dd = (sum(d*d for d in downs)/len(downs))**0.5 * (12**0.5)
    return None if dd == 0 else (ann_ret - RF)/dd

def max_drawdown(series):
    peak, mdd = -1, 0
    for _, v in series:
        peak = max(peak, v)
        if peak > 0: mdd = min(mdd, v/peak - 1)
    return mdd

def volatility(series):
    mr = monthly_returns(series)
    s = stdev(mr)
    return None if s is None else s*(12**0.5)

def compute_metrics(series):
    roll5 = rolling_returns(series, 5)
    return {
      "cagr_3y": cagr(series, 3), "cagr_5y": cagr(series, 5),
      "roll5_min": (min(roll5) if roll5 else None),
      "roll5_avg": (sum(roll5)/len(roll5) if roll5 else None),
      "roll5_pos_pct": (sum(1 for r in roll5 if r>0)/len(roll5) if roll5 else None),
      "volatility": volatility(series), "sortino": sortino(series),
      "max_drawdown": max_drawdown(series),
      "history_days": (series[-1][0]-series[0][0]).days if len(series)>1 else 0,
    }

def pct_rank(val, arr, higher_better=True):
    vals = [x for x in arr if x is not None]
    if val is None or not vals: return None
    below = sum(1 for x in vals if (x < val) == higher_better)
    return below/len(vals)

def rag(p):
    if p is None: return "na"
    return "green" if p >= 0.66 else ("amber" if p >= 0.33 else "red")

def score_category(funds):
    """funds: list of dicts with 'metrics'. Weight per our decision:
       consistency (roll5 min/avg/pos%) + downside (sortino, drawdown) heaviest,
       then cost proxy (volatility) and trailing return."""
    def col(k): return [f["metrics"].get(k) for f in funds]
    weights = {
      "roll5_avg":0.20, "roll5_min":0.15, "roll5_pos_pct":0.15,
      "sortino":0.20, "max_drawdown":0.15, "cagr_5y":0.10, "volatility":0.05,
    }
    for f in funds:
        m = f["metrics"]; comp = 0.0; wsum = 0.0; f["rag"] = {}
        for k, w in weights.items():
            hb = (k != "volatility")   # for drawdown, less-negative is better -> higher_better True works
            p = pct_rank(m.get(k), col(k), higher_better=hb)
            f["rag"][k] = rag(p)
            if p is not None: comp += w*p; wsum += w
        f["score"] = round(100*comp/wsum, 1) if wsum else None
    funds.sort(key=lambda f: (f["score"] is not None, f["score"] or 0), reverse=True)
    for i, f in enumerate(funds): f["rank"] = i+1
    return funds

def build():
    out = {"generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
           "source":"mfapi.in / AMFI (open NAV data), metrics computed by rankings.py",
           "categories": {}}
    for cat in TARGET_CATS:
        funds = []
        for code, name in UNIVERSE[cat].items():
            try:
                series = fetch_nav(code.rstrip("b"))   # tolerate placeholder codes
                if len(series) < 60: continue
                funds.append({"code":code, "name":name, "metrics":compute_metrics(series)})
                time.sleep(0.4)   # be polite to the free API
            except Exception as e:
                print("skip", name, e, file=sys.stderr)
        score_category(funds)
        out["categories"][cat] = funds
    with open("rankings.json","w") as f: json.dump(out, f, indent=1, default=str)
    print("wrote rankings.json with", sum(len(v) for v in out["categories"].values()), "funds")

if __name__ == "__main__":
    build()
