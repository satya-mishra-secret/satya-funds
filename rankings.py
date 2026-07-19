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
from concurrent.futures import ThreadPoolExecutor

MFAPI_LIST = "https://api.mfapi.in/mf"
MFAPI_ONE  = "https://api.mfapi.in/mf/%s"
RF         = 0.065     # risk-free proxy for Sortino
WORKERS    = 8         # parallel NAV fetches (I/O-bound; safe for free API)
TIMEOUT    = 15        # seconds per request (fail fast, do not hang)
TRIES      = 2         # attempts per request
TRIM_DAYS  = 2600      # keep ~7 yrs of NAV for 5-yr metrics (speeds compute)
PER_CAT_CAP= 50        # deep-score up to N per category (no serious fund sits below top 50)

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

# ------------------------------------------------------------------
# LAYER 2 (manual truth): subscription-status override list.
# "Is this fund open to fresh money?" is NOT in any free machine-readable feed,
# so it is maintained BY HAND here. Any fund NOT listed defaults to "open".
# Values: "closed" (no fresh money), "sip_only" (SIP ok, no lump-sum), "lump_ok".
# When you hear a fund restricted/reopened (it's always fund news), add/edit a line
# and bump STATUS_CHECKED to today's date -> the dashboard tracks that date and
# nags you if it goes stale.
# ------------------------------------------------------------------
STATUS_CHECKED = "2026-07-19"          # <-- update this whenever you review the list
STATUS_OVERRIDES = {
    "nippon india small cap": "sip_only",   # closed to lump-sum since Jul 2023 (verified)
    # "sbi small cap":        "sip_only",    # <- confirm & uncomment if restricted
    # "axis small cap":       "sip_only",    # <- confirm & uncomment if restricted
    # "<fund name substring>":"closed",       # <- add new restrictions here
}
def status_for(name):
    n=(name or "").lower()
    for key,val in STATUS_OVERRIDES.items():
        if key in n: return val
    return "open"

def norm(s):
    # normalise so "Mid-Cap" == "mid cap", collapse spaces
    return " ".join(s.lower().replace("-"," ").replace("&"," & ").split())

def get_json(url, timeout=TIMEOUT, tries=TRIES):
    last=None
    for _ in range(tries):
        try:
            req=Request(url, headers={"User-Agent":"rankings.py/2"})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last=e; time.sleep(0.4)
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
    out.sort(key=lambda x:x[0])
    if out:                                   # keep only recent history -> faster metrics
        cutoff=out[-1][0]-dt.timedelta(days=TRIM_DAYS)
        trimmed=[x for x in out if x[0]>=cutoff]
        if len(trimmed)>=250: out=trimmed
    return out

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

def _fetch_one(item):
    cat,code,name=item
    try:
        s=fetch_nav(code)
        if len(s)<250: return (cat,None,name,"short")
        m=metrics(s); ok,why=data_ok(cat,m)
        rec={"code":code,"name":name,"metrics":m,"status":status_for(name)}
        return (cat,(rec if ok else None),name,("" if ok else why))
    except Exception as e:
        return (cat,None,name,str(e)[:60])

def build():
    t0=time.time()
    buckets, total = discover()
    tasks=[]; discovered={}
    for cat, funds in buckets.items():
        seen=set(); cand=[]
        for code,name in funds:
            if code in seen: continue
            seen.add(code); cand.append((cat,code,name))
        discovered[cat]=len(cand)
        tasks += cand[:PER_CAT_CAP]
    print("discovered %d categories, %d funds to fetch (parallel x%d)"%(len(buckets),len(tasks),WORKERS),file=sys.stderr)

    results={c:[] for c in buckets}; flagged={c:[] for c in buckets}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for cat,rec,name,why in ex.map(_fetch_one, tasks):
            if rec is not None: results[cat].append(rec)
            elif why not in ("short",""): flagged[cat].append({"name":name,"why":why})

    out={"generated":dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
         "universe_entries_scanned":total,
         "status_checked":STATUS_CHECKED,
         "source":"mfapi.in / AMFI open NAV data; whole-universe auto-discovery; metrics by rankings.py v2",
         "stale_after_days":90,"categories":{},"meta":{}}
    for cat in buckets:
        sc=score(results[cat])
        out["categories"][cat]=sc
        out["meta"][cat]={"discovered":discovered[cat],"scored":len(sc),
                          "flagged":len(flagged[cat]),"flagged_detail":flagged[cat][:8]}
        print("%-16s discovered=%d scored=%d flagged=%d"%(cat,discovered[cat],len(sc),len(flagged[cat])),file=sys.stderr)
    with open("rankings.json","w") as f: json.dump(out,f,indent=1,default=str)
    print("done in %.1fs; scanned %d entries"%(time.time()-t0,total))

if __name__=="__main__": build()
