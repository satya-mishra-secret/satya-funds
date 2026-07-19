#!/usr/bin/env python3
"""
rankings.py  (v2)  —  WHOLE-UNIVERSE Indian mutual-fund ranking engine.

v2 changes vs v1:
  * AUTO-DISCOVERS the universe at runtime from mfapi.in (open AMFI data) instead
    of a hand-typed code list -> covers every Direct-Growth fund in each category,
    and removes the "wrong hardcoded code" corruption entirely.
  * DATA-SANITY GATE: rejects mismatched/IDCW series (e.g. an arbitrage fund
    showing a 5-yr loss, or an "index" fund with ~0 volatility) instead of
    silently scoring nonsense. Rejected funds are reported, not ranked.
  * TIE-AWARE PERCENTILE: fixes the bug where a metric that's identical across
    funds (e.g. roll5_pos_pct=1.0) dragged every score down. Constant metrics now
    score neutrally instead of red-for-all.

Only Python standard library. Runs on GitHub Actions / Colab / local.
"""
import json, sys, time, datetime as dt
from urllib.request import urlopen, Request
from urllib.error import URLError

MFAPI_LIST = "https://api.mfapi.in/mf"
MFAPI_ONE  = "https://api.mfapi.in/mf/%s"
RF         = 0.065     # risk-free proxy for Sortino
SLEEP      = 0.20      # politeness delay between NAV fetches (free API)
PER_CAT_CAP= 120       # safety cap on deep-scored funds per category

# category -> (must contain ALL of, must contain NONE of)   [lower-case name match]
CATEGORIES = {
  "Flexi Cap":       (["flexi cap"],            ["index","nifty","fund of fund","fof"]),
  "Large Cap":       (["large cap"],            ["mid","large & mid","large and mid","index","nifty"]),
  "Mid Cap":         (["mid cap"],              ["large","index","nifty","150","fof","fund of fund"]),
  "Small Cap":       (["small cap"],            ["index","nifty","250","fof","fund of fund"]),
  "Arbitrage":       (["arbitrage"],            ["fof","fund of fund"]),
  "Index Nifty 50":  (["nifty 50"],             ["next","equal","midcap","smallcap","bank","fof","500"]),
  "Index Nifty 500": (["nifty 500"],            ["fof","fund of fund"]),
}
EXCLUDE_PLANWORDS = ["idcw","dividend","payout","reinvest","bonus","regular"]

def norm(s):
    # normalise so "Mid-Cap" == "mid cap", collapse spaces
    return " ".join(s.lower().replace("-"," ").replace("&"," & ").split())

def get_json(url, timeout=90, tries=3):
    last=None
    for _ in range(tries):
        try:
            req=Request(url, headers={"User-Agent":"rankings.py/2"})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (URLError, TimeoutError, ValueError) as e:
            last=e; time.sleep(1.0)
    raise last

# ---------- discovery ----------
def discover():
    allschemes = get_json(MFAPI_LIST)
    buckets = {c: [] for c in CATEGORIES}
    for s in allschemes:
        name = (s.get("schemeName") or ""); low = norm(name)
        if "direct" not in low or "growth" not in low: continue
        if any(w in low for w in EXCLUDE_PLANWORDS): continue
        for cat,(inc,exc) in CATEGORIES.items():
            if all(k in low for k in inc) and not any(k in low for k in exc):
                buckets[cat].append((str(s.get("schemeCode")), name.split(" - ")[0].strip()))
                break
    return buckets, len(allschemes)

# ---------- NAV + metrics ----------
def fetch_nav(code):
    j = get_json(MFAPI_ONE % code)
    out=[]
    for row in j.get("data", []):
        try:
            out.append((dt.datetime.strptime(row["date"],"%d-%m-%Y").date(), float(row["nav"])))
        except Exception: pass
    out.sort(key=lambda x:x[0]); return out

def nav_before(series, target):
    lo,hi,res=0,len(series)-1,None
    while lo<=hi:
        m=(lo+hi)//2
        if series[m][0]<=target: res=series[m][1]; lo=m+1
        else: hi=m-1
    return res
def cagr(series, yrs):
    if not series: return None
    ed,ev=series[-1]; sv=nav_before(series, ed-dt.timedelta(days=int(365.25*yrs)))
    return None if (not sv or sv<=0) else (ev/sv)**(1/yrs)-1
def rolling(series, wy, step=30):
    if len(series)<2: return []
    ed=series[-1][0]; w=dt.timedelta(days=int(365.25*wy)); res=[]; cur=series[0][0]+w
    while cur<=ed:
        v0=nav_before(series,cur-w); v1=nav_before(series,cur)
        if v0 and v1 and v0>0: res.append((v1/v0)**(1/wy)-1)
        cur+=dt.timedelta(days=step)
    return res
def monthly(series):
    bm={}
    for d,v in series: bm[(d.year,d.month)]=v
    ks=sorted(bm); return [bm[ks[i]]/bm[ks[i-1]]-1 for i in range(1,len(ks)) if bm[ks[i-1]]>0]
def stdev(xs):
    if len(xs)<2: return None
    m=sum(xs)/len(xs); return (sum((x-m)**2 for x in xs)/(len(xs)-1))**0.5
def sortino(series):
    mr=monthly(series)
    if len(mr)<12: return None
    ann=(1+sum(mr)/len(mr))**12-1; downs=[min(0,r-RF/12) for r in mr]
    dd=(sum(d*d for d in downs)/len(downs))**0.5*(12**0.5)
    return None if dd==0 else (ann-RF)/dd
def maxdd(series):
    peak,md=-1,0
    for _,v in series:
        peak=max(peak,v)
        if peak>0: md=min(md,v/peak-1)
    return md
def vol(series):
    s=stdev(monthly(series)); return None if s is None else s*(12**0.5)
def metrics(series):
    r5=rolling(series,5)
    return {"cagr_3y":cagr(series,3),"cagr_5y":cagr(series,5),
      "roll5_min":(min(r5) if r5 else None),"roll5_avg":(sum(r5)/len(r5) if r5 else None),
      "roll5_pos_pct":(sum(1 for r in r5 if r>0)/len(r5) if r5 else None),
      "volatility":vol(series),"sortino":sortino(series),"max_drawdown":maxdd(series),
      "history_days":(series[-1][0]-series[0][0]).days if len(series)>1 else 0}

# ---------- sanity gate ----------
def data_ok(cat, m):
    if m["history_days"] < 400: return False, "insufficient history"
    c5=m["cagr_5y"]
    if "Arbitrage" in cat:
        if m["max_drawdown"] is not None and m["max_drawdown"] < -0.06: return False,"drawdown too deep for arbitrage (bad plan?)"
        if m["volatility"] is not None and m["volatility"] > 0.06: return False,"volatility too high for arbitrage (bad plan?)"
        if c5 is not None and c5 < 0.02: return False,"return too low for arbitrage (IDCW plan?)"
    elif "Index" in cat:
        if m["volatility"] is not None and m["volatility"] < 0.05: return False,"volatility ~0 (IDCW/bad series?)"
        if c5 is not None and c5 < 0: return False,"negative 5y (bad series?)"
    else:
        if c5 is not None and c5 < 0: return False,"negative 5y for equity (IDCW/bad series?)"
    return True, ""

# ---------- scoring (tie-aware percentile) ----------
def pct_rank(val, arr, higher=True):
    vals=[x for x in arr if x is not None]
    if val is None or len(vals)<2: return 0.5
    worse=sum(1 for x in vals if (x<val)==higher); ties=sum(1 for x in vals if x==val)
    return (worse+0.5*ties)/len(vals)
def rag(p): return "na" if p is None else ("green" if p>=0.66 else ("amber" if p>=0.33 else "red"))
WEIGHTS={"roll5_avg":.20,"roll5_min":.15,"roll5_pos_pct":.10,"sortino":.20,"max_drawdown":.15,"cagr_5y":.12,"volatility":.08}
def score(funds):
    def col(k): return [f["metrics"].get(k) for f in funds]
    for f in funds:
        m=f["metrics"]; comp=w=0.0; f["rag"]={}
        for k,wt in WEIGHTS.items():
            p=pct_rank(m.get(k), col(k), higher=(k!="volatility"))
            f["rag"][k]=rag(p); comp+=wt*p; w+=wt
        f["score"]=round(100*comp/w,1) if w else None
    funds.sort(key=lambda f:(f["score"] is not None, f["score"] or 0), reverse=True)
    for i,f in enumerate(funds): f["rank"]=i+1
    return funds

def build():
    buckets, total = discover()
    out={"generated":dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
         "universe_entries_scanned":total,
         "source":"mfapi.in / AMFI open NAV data; whole-universe auto-discovery; metrics by rankings.py v2",
         "stale_after_days":90,"categories":{},"meta":{}}
    for cat, funds in buckets.items():
        seen=set(); cand=[]
        for code,name in funds:
            if code in seen: continue
            seen.add(code); cand.append((code,name))
        scored=[]; flagged=[]
        for code,name in cand[:PER_CAT_CAP]:
            try:
                s=fetch_nav(code); time.sleep(SLEEP)
                if len(s)<60: continue
                m=metrics(s); ok,why=data_ok(cat,m)
                if ok: scored.append({"code":code,"name":name,"metrics":m})
                else:  flagged.append({"name":name,"why":why})
            except Exception as e:
                print("skip",name,e,file=sys.stderr)
        score(scored)
        out["categories"][cat]=scored
        out["meta"][cat]={"discovered":len(cand),"scored":len(scored),"flagged":len(flagged),"flagged_detail":flagged[:8]}
        print("%-16s discovered=%d scored=%d flagged=%d"%(cat,len(cand),len(scored),len(flagged)),file=sys.stderr)
    with open("rankings.json","w") as f: json.dump(out,f,indent=1,default=str)
    print("wrote rankings.json; scanned",total,"scheme entries")

if __name__=="__main__": build()
