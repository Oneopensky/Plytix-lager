#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Opdater Plytix 'samlet_lager' fra en Matrixify Shopify-eksport.  (hurtig, fil-drevet)
 
Sådan virker den:
  1) Henter CSV (lokalt via CSV_PATH, eller fra Hetzner-boksen via SFTP hvis SFTP_HOST er sat)
  2) Bygger {variant-SKU: samlet lager paa tvaers af valgte lokationer}
  3) Slaar filens SKU'er op i Plytix:
       SINGLE  -> skriver produktets eget lager
       VARIANT -> skriver barnets eget lager  (og laegges til dets parents sum)
  4) Henter alle PARENT-produkter og skriver summen af deres boerns lager
  5) Springer produkter over hvis vaerdien er uaendret. Skriver kun ved DRY_RUN=0.
 
Miljovariabler:
  PLX_KEY, PLX_PWD        (kraevet)
  DRY_RUN=1               1=vis kun plan (standard), 0=skriv rigtigt
  TARGET_FIELD=samlet_lager
  EXCLUDE_LOCATIONS=      komma-liste; lokationer hvis navn indeholder et ord udelades
  Filkilde - vaelg én:
    CSV_PATH             sti til en lokal Matrixify-fil
    ELLER SFTP_HOST + SFTP_USER + SFTP_PASS + SFTP_FILE   (henter fra boksen)
"""
import os, json, sys, csv, time, tempfile, urllib.request, urllib.error
 
KEY=os.environ.get("PLX_KEY","").strip(); PWD=os.environ.get("PLX_PWD","").strip()
CSV_PATH=os.environ.get("CSV_PATH","").strip()
TARGET=os.environ.get("TARGET_FIELD","samlet_lager").strip()
DRY=os.environ.get("DRY_RUN","1")!="0"
EXCL=[s.strip().lower() for s in os.environ.get("EXCLUDE_LOCATIONS","").split(",") if s.strip()]
SFTP_HOST=os.environ.get("SFTP_HOST","").strip()
if not KEY or not PWD: sys.exit("Saet PLX_KEY og PLX_PWD.")
 
# ---------- Hent fil fra boksen via SFTP hvis relevant ----------
if SFTP_HOST and not (CSV_PATH and os.path.exists(CSV_PATH)):
    import paramiko, stat as _stat
    local=os.path.join(tempfile.gettempdir(),"shopify_lager.csv")
    t=paramiko.Transport((SFTP_HOST,22))
    t.connect(username=os.environ["SFTP_USER"],password=os.environ["SFTP_PASS"])
    s=paramiko.SFTPClient.from_transport(t)
    remote=os.environ["SFTP_FILE"]
    # Hvis SFTP_FILE peger paa en mappe, tag den nyeste CSV deri
    try: is_dir=_stat.S_ISDIR(s.stat(remote).st_mode)
    except IOError: is_dir=remote.endswith("/")
    if is_dir:
        d=remote if remote.endswith("/") else remote+"/"
        files=[e for e in s.listdir_attr(d) if e.filename.lower().endswith(".csv")]
        if not files: sys.exit("Ingen CSV-fil fundet i mappen "+d)
        newest=max(files,key=lambda e:e.st_mtime)
        remote=d+newest.filename
        print("Nyeste CSV i mappen:",newest.filename)
    s.get(remote,local); s.close(); t.close()
    CSV_PATH=local
    print("Hentede fil via SFTP:",remote)
if not CSV_PATH or not os.path.exists(CSV_PATH):
    sys.exit("Saet CSV_PATH (lokal fil) eller SFTP_HOST/USER/PASS/FILE (boksen).")
 
BASE="https://pim.plytix.com/api/v1"
_tok=[None]
def _raw(m,u,p=None,bearer=True):
    d=json.dumps(p).encode() if p is not None else None
    r=urllib.request.Request(u,data=d,method=m); r.add_header("Content-Type","application/json")
    if bearer and _tok[0]: r.add_header("Authorization","Bearer "+_tok[0])
    try:
        with urllib.request.urlopen(r,timeout=60) as x: return x.status,json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw=e.read().decode()
        try: raw=json.loads(raw)
        except Exception: pass
        return e.code,raw
def auth():
    st,b=_raw("POST","https://auth.plytix.com/auth/api/get-token",
              {"api_key":KEY,"api_password":PWD},bearer=False)
    if st!=200: sys.exit("Auth fejl: %s"%b)
    _tok[0]=b["data"][0]["access_token"]
def call(m,u,p=None,_retry=True):
    st,b=_raw(m,u,p)
    if st==401 and _retry: auth(); return call(m,u,p,_retry=False)
    if st==429: time.sleep(2); return call(m,u,p,_retry=False)
    return st,b
 
def to_int(x):
    if x is None: return 0
    s=str(x).strip().replace(" ","")
    if s in ("","-"): return 0
    try: return int(float(s.replace(",",".")))
    except Exception: return 0
 
def cur_val(pr):
    a=pr.get("attributes") or {}
    if TARGET in a: return a[TARGET]
    return pr.get(TARGET)
 
def read_matrixify(path):
    with open(path,"r",encoding="utf-8-sig",newline="") as f:
        rows=[r for r in csv.reader(f) if r]
    if rows and max(len(r) for r in rows[:5])==1:
        rows=[next(csv.reader([r[0]])) for r in rows]
    return (rows[0],rows[1:]) if rows else ([],[])
 
# ---------- 1) Laes filen ----------
header,datarows=read_matrixify(CSV_PATH)
sku_col=next((h for h in header if h and h.strip().lower() in ("variant sku","sku")),None) \
        or next((h for h in header if h and "sku" in h.lower()),None)
all_inv=[h for h in header if h and h.strip().lower().startswith("inventory available")]
inv_cols=[h for h in all_inv if not any(w in h.lower() for w in EXCL)]
print("Match-noegle:",sku_col)
print("Lokationer der TAELLES med:", [h.split(":",1)[1].strip() for h in inv_cols])
if [h for h in all_inv if h not in inv_cols]:
    print("Udeladt:", [h.split(":",1)[1].strip() for h in all_inv if h not in inv_cols])
if not sku_col or not inv_cols: sys.exit("Stop: mangler SKU- eller lager-kolonner.")
stock={}
for r in datarows:
    d=dict(zip(header,r)); k=(d.get(sku_col) or "").strip()
    if k: stock[k]=stock.get(k,0)+sum(to_int(d.get(c)) for c in inv_cols)
print("Laeste %d unikke SKU'er fra filen." % len(stock))
 
auth()
plan=[]; parent_sum={}; skus=list(stock)
 
# ---------- 2) Direkte SKU-match (SINGLE + VARIANT) ----------
print("\nSlaar filens SKU'er op i Plytix...")
writes=0
for i in range(0,len(skus),40):
    batch=skus[i:i+40]
    st,b=call("POST",BASE+"/products/search",
              {"attributes":["sku","label","product_type","num_variations","product_family_model_id",TARGET],
               "filters":[[{"field":"sku","operator":"in","value":batch}]],
               "pagination":{"page":1,"page_size":100}})
    for pr in (b.get("data",[]) if isinstance(b,dict) else []):
        sku=(pr.get("sku") or "").strip(); ptype=pr.get("product_type")
        mid=pr.get("product_family_model_id"); val=stock.get(sku,0)
        if ptype=="VARIANT" and mid:
            parent_sum[mid]=parent_sum.get(mid,0)+val
        cur=cur_val(pr)
        if cur is None or to_int(cur)!=val:
            plan.append((pr["id"], pr.get("label") or sku, val)); writes+=1
    print("  ...%d/%d SKU'er slaaet op, %d aendringer indtil nu" % (min(i+40,len(skus)),len(skus),writes))
 
# ---------- 3) PARENT-produkter: sum af boern ----------
print("\nHenter PARENT-produkter...")
page=1; parents=0
while True:
    st,b=call("POST",BASE+"/products/search",
              {"attributes":["sku","label","product_family_model_id",TARGET],
               "filters":[[{"field":"product_type","operator":"eq","value":"PARENT"}]],
               "pagination":{"page":page,"page_size":100}})
    data=b.get("data",[]) if isinstance(b,dict) else []
    if not data: break
    for pr in data:
        parents+=1
        mid=pr.get("product_family_model_id")
        if mid in parent_sum:
            val=parent_sum[mid]; cur=cur_val(pr)
            if cur is None or to_int(cur)!=val:
                plan.append((pr["id"], (pr.get("label") or pr.get("sku"))+" [PARENT]", val))
    page+=1
print("  gennemgik %d PARENT-produkter." % parents)
 
# ---------- 4) Resultat / skrivning ----------
print("\n=== PLAN ===")
print("Produkter der skal opdateres:", len(plan))
for pid,label,val in plan[:15]:
    print("  %-40s %s = %s" % (label[:40], TARGET, val))
if len(plan)>15: print("  ... (+%d flere)" % (len(plan)-15))
 
if DRY:
    print("\nDRY_RUN=1: intet skrevet. Saet DRY_RUN=0 for at skrive rigtigt.")
    sys.exit(0)
 
print("\nSkriver til Plytix...")
ok=err=0
for i,(pid,label,val) in enumerate(plan,1):
    st,b=call("PATCH",BASE+"/products/%s"%pid,{"attributes":{TARGET:val}})
    if st in (200,201): ok+=1
    else:
        err+=1
        if err<=5: print("  FEJL",st,label,b)
    if i%50==0: print("  ...%d/%d (ok=%d fejl=%d)"%(i,len(plan),ok,err))
print("Faerdig. Skrevet ok=%d, fejl=%d." % (ok,err))
