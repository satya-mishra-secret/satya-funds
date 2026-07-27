#!/usr/bin/env python3
"""
rankings.py  (v3)  —  WHOLE-UNIVERSE Indian MF ranking + CONVICTION engine.

v3 adds, on top of v2's whole-universe auto-discovery + sanity gate + tie-aware
percentile scoring:
  * BENCHMARK PROXY per category: a low-cost index fund's Direct-Growth NAV,
    discovered from the same mfapi feed (no scraping) -> the "official index"
    comparison line and the basis for capture + regime detection. Hybrids
    (Equity Savings, Arbitrage) and the Index categories fall back to
    category-only comparison.
  * AUTO-DETECTED REGIMES: peak-to-trough drawdowns >=10% on the benchmark are
    found automatically and shared across all funds in the category, so
    "how did it do in the bad phase" is comparable and always current.
  * CAPTURE: up/down-capture vs the benchmark (how much of the rise/fall a fund
    takes) -> the downside lens.
  * CONVICTION score (downside + consistency + capture, low weight on trailing
    return) and a HEAT flag (how recency-driven the fund's standing is). The two
    together are the anti-momentum-chasing guardrail.
  * Plain-English VERDICT built from those numbers, plus compact rebased NAV
    series (fund / category / index) for the comparison graph.

Only Python standard library. Emits rankings.json. The analytics core
(drawdowns / capture / conviction / heat / verdict) is unit-tested separately;
the fetch/discovery layer is validated by a GitHub Actions run.
"""
import json, sys, time, datetime as dt
from urllib.request import urlopen, Request
from concurrent.futures import ThreadPoolExecutor

MFAPI_LIST = "https://api.mfapi.in/mf"
MFAPI_ONE  = "https://api.mfapi.in/mf/%s"
RF         = 0.065
WORKERS    = 8
TIMEOUT    = 15
TRIES      = 2
TRIM_DAYS  = 2600
STALE_DAYS = 45
PER_CAT_CAP= 50
DD_THRESH  = 0.10          # a fall >=10% peak-to-trough defines a "bad phase"

CATEGORIES = {
  "Flexi Cap":        (["flexi cap"],            ["index","nifty","fund of fund","fof"]),
  "Large Cap":        (["large cap"],            ["mid","large & mid","large and mid","index","nifty"]),
  "Large & Mid Cap":  (["large","mid"],          ["index","nifty","fof","fund of fund","150","250","small"]),
  "Mid Cap":          (["mid cap"],              ["large","index","nifty","150","fof","fund of fund"]),
  "Small Cap":        (["small cap"],            ["index","nifty","250","fof","fund of fund"]),
  "Focused":          (["focused"],              ["index","nifty","fof","fund of fund"]),
  "Contra":           (["contra"],               ["index","nifty","fof","fund of fund"]),
  "Equity Savings":   (["equity savings"],       ["index","nifty","fof","fund of fund"]),
  "Arbitrage":        (["arbitrage"],            ["fof","fund of fund"]),
  "Index Nifty 50":   (["nifty 50"],             ["next","equal","midcap","smallcap","bank","fof","500"]),
  "Index Nifty 500":  (["nifty 500"],            ["fof","fund of fund"]),
}
# category -> benchmark index fund to discover (Direct-Growth NAV = TRI minus a small fee).
# None  -> category-only comparison (hybrids / the index categories themselves).
BENCHMARKS = {
  "Flexi Cap":       ["nifty 500","index"],
  "Large Cap":       ["nifty 100","index"],
  "Large & Mid Cap": ["nifty largemidcap 250","index"],
  "Mid Cap":         ["nifty midcap 150","index"],
  "Small Cap":       ["nifty smallcap 250","index"],
  "Focused":         ["nifty 500","index"],
  "Contra":          ["nifty 500","index"],
  "Equity Savings":  None,
  "Arbitrage":       None,
  "Index Nifty 50":  None,
  "Index Nifty 500": None,
}
EXCLUDE_PLANWORDS = ["idcw","dividend","payout","reinvest","bonus","regular"]

STATUS_CHECKED = "2026-07-19"
STATUS_OVERRIDES = {
    "nippon india small cap": "sip_only",
}
def status_for(name):
    n=(name or "").lower()
    for k,v in STATUS_OVERRIDES.items():
        if k in n: return v
    return "open"

def norm(s):
    return " ".join(s.lower().replace("-"," ").replace("&"," & ").split())

def get_json(url, timeout=TIMEOUT, tries=TRIES):
    last=None
    for _ in range(tries):
        try:
            req=Request(url, headers={"User-Agent":"rankings.py/3"})
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
        name=(s.get("schemeName") or ""); low=norm(name)
        if "direct" not in low or "growth" not in low: continue
        if any(w in low for w in EXCLUDE_PLANWORDS): continue
        for cat,(inc,exc) in CATEGORIES.items():
            if all(k in low for k in inc) and not any(k in low for k in exc):
                buckets[cat].append((str(s.get("schemeCode")), name.split(" - ")[0].strip())); break
    bench={}
    for cat,pat in BENCHMARKS.items():
        if not pat: continue
        for s in allschemes:
            name=(s.get("schemeName") or ""); low=norm(name)
            if "direct" not in low or "growth" not in low: continue
            if any(w in low for w in EXCLUDE_PLANWORDS): continue
            if all(k in low for k in pat):
                bench[cat]=(str(s.get("schemeCode")), name.split(" - ")[0].strip()); break
    return buckets, bench, len(allschemes)

def fetch_nav(code):
    j=get_json(MFAPI_ONE % code); out=[]
    for row in j.get("data", []):
        try: out.append((dt.datetime.strptime(row["date"],"%d-%m-%Y").date(), float(row["nav"])))
        except Exception: pass
    out.sort(key=lambda x:x[0])
    if out:
        cutoff=out[-1][0]-dt.timedelta(days=TRIM_DAYS)
        t=[x for x in out if x[0]>=cutoff]
        if len(t)>=250: out=t
    return out

# ---------- basic metrics (v2) ----------
def nav_before(series,target):
    lo,hi,res=0,len(series)-1,None
    while lo<=hi:
        m=(lo+hi)//2
        if series[m][0]<=target: res=series[m][1]; lo=m+1
        else: hi=m-1
    return res
def cagr(series,yrs):
    if not series: return None
    ed,ev=series[-1]; sv=nav_before(series,ed-dt.timedelta(days=int(365.25*yrs)))
    return None if (not sv or sv<=0) else (ev/sv)**(1/yrs)-1
def rolling(series,wy,step=30):
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
    return {"cagr_1y":cagr(series,1),"cagr_3y":cagr(series,3),"cagr_5y":cagr(series,5),
      "roll5_min":(min(r5) if r5 else None),"roll5_avg":(sum(r5)/len(r5) if r5 else None),
      "roll5_pos_pct":(sum(1 for r in r5 if r>0)/len(r5) if r5 else None),
      "volatility":vol(series),"sortino":sortino(series),"max_drawdown":maxdd(series),
      "history_days":(series[-1][0]-series[0][0]).days if len(series)>1 else 0}

# ---------- monthly grid helpers (for capture / regimes / series) ----------
def month_grid(series):
    bm={}
    for d,v in series: bm[(d.year,d.month)]=v
    ks=sorted(bm); return [(y*12+(m-1), bm[(y,m)]) for (y,m) in ks]
def ym_label(ym): return "%d-%02d"%(ym//12, ym%12+1)
def align_rets(fg,bg):
    fd=dict(fg); bd=dict(bg); common=sorted(set(fd)&set(bd))
    fr=[]; br=[]
    for i in range(1,len(common)):
        a,b=common[i-1],common[i]
        if fd[a]>0 and bd[a]>0: fr.append(fd[b]/fd[a]-1); br.append(bd[b]/bd[a]-1)
    return fr,br
def capture(fr,br,up=True):
    ff=bb=1.0; n=0
    for f,b in zip(fr,br):
        if (b>0)==up: ff*=(1+f); bb*=(1+b); n+=1
    if n==0 or abs(bb-1)<1e-9: return None
    return round((ff-1)/(bb-1),3)
def drawdowns(grid,thresh=DD_THRESH):
    if len(grid)<3: return []
    wins=[]; peak=grid[0][1]; peakYM=grid[0][0]; inDD=False; startYM=peakYM; tv=peak; tYM=peakYM
    for ym,v in grid:
        if not inDD and v>peak: peak=v; peakYM=ym
        if not inDD and peak>0 and v/peak-1<=-thresh: inDD=True; startYM=peakYM; tv=v; tYM=ym
        if inDD:
            if v<tv: tv=v; tYM=ym
            if v>=peak: wins.append((startYM,tYM,ym, round(tv/peak-1,3))); inDD=False; peak=v; peakYM=ym
    if inDD: wins.append((startYM,tYM,None, round(tv/peak-1,3)))
    return wins
def win_label(a,b):
    ya,yb=a//12,b//12
    return str(ya) if ya==yb else "%d-%02d"%(ya,yb%100)
def ret_between(grid, a, b):
    d=dict(grid)
    if a in d and b in d and d[a]>0: return d[b]/d[a]-1
    return None
def rebase_q(grid, anchor=None):
    if not grid: return []
    base=grid[0][1] if anchor is None else anchor
    out=[]
    for i,(ym,v) in enumerate(grid):
        if i%3==0 and base>0: out.append([ym_label(ym), round(v/base*100,1)])
    return out

# ---------- sanity gate (v2, extended cats pass through 'else') ----------
def data_ok(cat,m):
    if m["history_days"]<400: return False,"insufficient history"
    c5=m["cagr_5y"]
    if "Arbitrage" in cat:
        if m["max_drawdown"] is not None and m["max_drawdown"]<-0.06: return False,"drawdown too deep for arbitrage"
        if m["volatility"] is not None and m["volatility"]>0.06: return False,"volatility too high for arbitrage"
        if c5 is not None and c5<0.02: return False,"return too low for arbitrage (IDCW?)"
    elif "Index" in cat:
        if m["volatility"] is not None and m["volatility"]<0.05: return False,"volatility ~0 (bad series?)"
        if c5 is not None and c5<0: return False,"negative 5y (bad series?)"
    else:
        if c5 is not None and c5<0: return False,"negative 5y for equity (bad series?)"
    return True,""

# ---------- scoring ----------
def pct_rank(val,arr,higher=True):
    vals=[x for x in arr if x is not None]
    if val is None or len(vals)<2: return 0.5
    worse=sum(1 for x in vals if (x<val)==higher); ties=sum(1 for x in vals if x==val)
    return (worse+0.5*ties)/len(vals)
def rag(p): return "na" if p is None else ("green" if p>=0.66 else ("amber" if p>=0.33 else "red"))
RAG_METRICS={"roll5_avg":True,"roll5_min":True,"sortino":True,"max_drawdown":True,"cagr_5y":True,"volatility":False}
CONV_W={"down_capture":.22,"max_drawdown":.15,"roll5_min":.13,"sortino":.15,
        "roll5_beat_pct":.12,"up_capture":.08,"cagr_5y":.07,"volatility":.08}
CONV_LOWER={"down_capture","volatility"}

def verdict(c,h,dc):
    if c>=70: b="Solid and consistent"
    elif c>=45: b="Middling on the measures that last"
    else: b="Weak on the measures that last"
    if dc is not None and dc<0.90: b+=" \u2014 cushions well when markets fall"
    elif dc is not None and dc>1.02: b+=" \u2014 falls as hard as the market or harder"
    if h>=75 and c<60: b+=". Running hot on recent gains \u2014 be cautious."
    elif h>=75: b+=". Strong, but hot lately \u2014 some of this is recent momentum."
    else: b+="."
    return b

# ---------- per-category analytics ----------
def analyse(cat, funds, bench_series):
    # funds: list of dicts each with 'nav'(series),'metrics','name','code','status'
    bench_grid = month_grid(bench_series) if bench_series else None
    grids={f["code"]:month_grid(f["nav"]) for f in funds}
    # category average series (rebase each fund to 100 at its first common month, average)
    allmonths=set()
    for g in grids.values(): allmonths|={ym for ym,_ in g}
    months=sorted(allmonths)
    cat_series=[]
    for ym in months:
        vals=[]
        for g in grids.values():
            d=dict(g)
            if not d: continue
            first=min(d); 
            if ym in d and d[first]>0: vals.append(d[ym]/d[first]*100)
        if vals: cat_series.append((ym, sum(vals)/len(vals)))
    regime_src = bench_grid if bench_grid else cat_series
    regimes = drawdowns(regime_src, DD_THRESH)
    reg_meta=[{"label":win_label(a, (b if r is None else b)),"from":ym_label(a),
               "trough":ym_label(b),"recover":(ym_label(r) if r else None),"fall":fall}
              for (a,b,r,fall) in regimes]
    # category return within each regime window (peak->trough)
    cat_reg={}
    catd=dict(cat_series)
    for (a,b,r,fall) in regimes:
        rr=ret_between(cat_series,a,b); cat_reg[(a,b)]=rr
    # per-fund analytics
    for f in funds:
        g=grids[f["code"]]; m=f["metrics"]
        if bench_grid:
            fr,br=align_rets(g,bench_grid)
            f["capture"]={"down":capture(fr,br,up=False),"up":capture(fr,br,up=True)}
        else:
            f["capture"]={"down":None,"up":None}
        # regime scores: fund vs category in each window
        rs={}
        for (a,b,r,fall) in regimes:
            fr_=ret_between(g,a,b); cr_=cat_reg.get((a,b))
            rs[win_label(a,b)] = (round(fr_-cr_,3) if (fr_ is not None and cr_ is not None) else None)
        f["regime_scores"]=rs
        # persistence: % of 3y/5y rolling windows beating the category average
        f["persistence"]={"roll3_beat_pct":roll_beat(g,cat_series,3),
                          "roll5_beat_pct":roll_beat(g,cat_series,5)}
        f["nav_rebased"]=rebase_q(g)
    # conviction inputs across the category
    def col(k): return [feat(f,k) for f in funds]
    cols={k:col(k) for k in CONV_W}
    heatcol=[f["metrics"].get("cagr_1y") for f in funds]
    for f in funds:
        comp=w=0.0; f["rag"]={}
        for k,tf in RAG_METRICS.items():
            f["rag"][k]=rag(pct_rank(f["metrics"].get(k),[x["metrics"].get(k) for x in funds],higher=tf))
        for k,wt in CONV_W.items():
            p=pct_rank(feat(f,k),cols[k],higher=(k not in CONV_LOWER)); comp+=wt*p; w+=wt
        f["conviction"]=round(100*comp/w,1) if w else None
        f["score"]=f["conviction"]   # alias so the current dashboard reads scored mode
        f["heat"]=round(100*pct_rank(f["metrics"].get("cagr_1y"),heatcol),0)
        dc=f["capture"]["down"]
        f["verdict"]=verdict(f["conviction"] or 0, f["heat"] or 0, dc)
    funds.sort(key=lambda f:(f["conviction"] is not None, f["conviction"] or 0), reverse=True)
    for i,f in enumerate(funds): f["rank"]=i+1
    bench_out=None
    if bench_series:
        bg=month_grid(bench_series); bench_out=rebase_q(bg)
    return {"index_series":bench_out,"category_series":rebase_q(cat_series),
            "regimes":reg_meta}, funds

def feat(f,k):
    if k in ("down_capture",): return f.get("capture",{}).get("down")
    if k in ("up_capture",):   return f.get("capture",{}).get("up")
    if k=="roll5_beat_pct":    return f.get("persistence",{}).get("roll5_beat_pct")
    return f["metrics"].get(k)
def roll_beat(fg,cat_series,yrs):
    fd=dict(fg); cd=dict(cat_series); w=yrs*12
    common=sorted(set(fd)&set(cd)); wins=beat=0
    for i in range(len(common)):
        a=common[i]; b=a+w
        if b in fd and b in cd and fd[a]>0 and cd[a]>0:
            fr=(fd[b]/fd[a]); cr=(cd[b]/cd[a])
            wins+=1; beat+=(1 if fr>cr else 0)
    return round(beat/wins,3) if wins else None

# ---------- build ----------
def _fetch_one(item):
    cat,code,name=item
    try:
        s=fetch_nav(code)
        if len(s)<250: return (cat,None,name,"short")
        if (dt.date.today()-s[-1][0]).days>STALE_DAYS: return (cat,None,name,"stale NAV")
        m=metrics(s); ok,why=data_ok(cat,m)
        rec={"code":code,"name":name,"metrics":m,"status":status_for(name),"nav":s}
        return (cat,(rec if ok else None),name,("" if ok else why))
    except Exception as e:
        return (cat,None,name,str(e)[:60])

def build():
    t0=time.time()
    buckets,bench,total=discover()
    tasks=[]; discovered={}
    for cat,funds in buckets.items():
        seen=set(); cand=[]
        for code,name in funds:
            if code in seen: continue
            seen.add(code); cand.append((cat,code,name))
        discovered[cat]=len(cand); tasks+=cand[:PER_CAT_CAP]
    bench_series={}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # benchmark NAVs
        bfut={cat:ex.submit(fetch_nav,code) for cat,(code,_n) in bench.items()}
        for cat,fu in bfut.items():
            try: bench_series[cat]=fu.result()
            except Exception: bench_series[cat]=None
    results={c:[] for c in buckets}; flagged={c:[] for c in buckets}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for cat,rec,name,why in ex.map(_fetch_one,tasks):
            if rec is not None: results[cat].append(rec)
            elif why not in ("short",""): flagged[cat].append({"name":name,"why":why})
    out={"generated":dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
         "engine":"rankings.py v3 (conviction+capture+regimes)",
         "universe_entries_scanned":total,"status_checked":STATUS_CHECKED,
         "source":"mfapi.in/AMFI NAV; benchmark = low-cost index-fund NAV proxy; metrics by rankings.py v3",
         "stale_after_days":90,"categories":{},"benchmarks":{},"meta":{}}
    for cat in buckets:
        bmeta,funds = analyse(cat, results[cat], bench_series.get(cat))
        for f in funds: f.pop("nav",None)   # drop raw series from output
        out["categories"][cat]=funds
        out["benchmarks"][cat]={"index_name":(bench.get(cat,[None,None])[1] if bench.get(cat) else None),
                                **bmeta}
        out["meta"][cat]={"discovered":discovered[cat],"scored":len(funds),
                          "flagged":len(flagged[cat]),"flagged_detail":flagged[cat][:8]}
        print("%-16s scored=%d flagged=%d bench=%s"%(cat,len(funds),len(flagged[cat]),
              "yes" if bench_series.get(cat) else "category-only"),file=sys.stderr)
    with open("rankings.json","w") as f: json.dump(out,f,indent=1,default=str)
    print("done in %.1fs; scanned %d"%(time.time()-t0,total))

if __name__=="__main__": build()
