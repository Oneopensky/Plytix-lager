#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Opdater Plytix lager-felter fra en Matrixify Shopify-eksport.  (hurtig, fil-drevet)
 
Skriver:
  - samlet_lager (TARGET_FIELD): internt lager = summen af de INKLUDEREDE lokationer
  - fjernlager_antal (FJERNLAGER_FIELD, valgfrit): lager paa Fjernlager-lokationen
 
To tilstande:
  - Normal (GitHub/dagligt): skriver aendringer til Plytix via API (skipper uaendrede).
  - OUTPUT_CSV sat: skriver i stedet en import-fil (SKU + felter) til bulk-import.
 
Logik:
  SINGLE/VARIANT -> produktets/barnets egne tal
  PARENT         -> summen af DE EGNE boern (barn -> parent via SKU: KBKI-..-SB_9 -> KBKI-..-SB)
 
Miljovariabler:
  PLX_KEY, PLX_PWD        (kraevet)
  DRY_RUN=1               1=vis kun plan, 0=skriv rigtigt (ignoreres hvis OUTPUT_CSV)
  OUTPUT_CSV=sti          skriv import-fil i stedet for API
  TARGET_FIELD=samlet_lager
  FJERNLAGER_FIELD=       navn paa fjernlager-attribut (tomt = spring fjernlager over)
  FJERNLAGER_LOCATION=Fjernlager   lokationsnavnet der bruges til fjernlager-feltet
  EXCLUDE_LOCATIONS=      komma-liste; lokationer der udelades fra samlet_lager
  Filkilde: CSV_PATH (lokal fil)  ELLER  SFTP_HOST + SFTP_USER + SFTP_PASS + SFTP_FILE
"""
import os, json, sys, csv, time, tempfile, urllib.request, urllib.error
 
KEY=os.environ.get("PLX_KEY","").strip(); PWD=os.environ.get("PLX_PWD","").strip()
CSV_PATH=os.environ.get("CSV_PATH","").strip()
TARGET=os.environ.get("TARGET_FIELD","samlet_lager").strip()
FJ_FIELD=os.environ.get("FJERNLAGER_FIELD","").strip()
FJ_LOC=os.environ.get("FJERNLAGER_LOCATION","Fjernlager").strip().lower()
DRY=os.environ.get("DRY_RUN","1")!="0"
OUTPUT_CSV=os.environ.get("OUTPUT_CSV","").strip()
EXCL=[s.strip().lower() for s in os.environ.get("EXCLUDE_LOCATIONS","").split(",") if s.strip()]
SFTP_HOST=os.environ.get("SFTP_HOST","").strip()
if not KEY or not PWD: sys.exit("Saet PLX_KEY og PLX_PWD.")
 
if SFTP_HOST and not (CSV_PATH and os.path.exists(CSV_PATH)):
    import paramiko, stat as _stat
    local=os.path.join(tempfile.gettempdir(),"shopify_lager.csv")
    t=paramiko.Transport((SFTP_HOST,22))
    t.connect(username=os.environ["SFTP_USER"],password=os.environ["SFTP_PASS"])
    s=paramiko.SFTPClient.from_transport(t)
    remote=os.environ["SFTP_FILE"]
    try: is_dir=_stat.S_ISDIR(s.stat(remote).st_mode)
    except IOError: is_dir=remote.endswith("/")
    if is_dir:
        d=remote if remote.endswith("/") else remote+"/"
        files=[e for e in s.listdir_attr(d) if e.filename.lower().endswith(".csv")]
        if not files: sys.exit("Ingen CSV-fil fundet i mappen "+d)
        newest=max(files,key=lambda e:e.st_mtime); remote=d+newest.filename
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
 
def cur_val(pr,field):
    a=pr.get("attributes") or {}
    if field in a: return a[field]
    return pr.get(field)
 
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
fj_cols=[h for h in all_inv if FJ_LOC in h.lower()] if FJ_FIELD else []
print("Match-noegle:",sku_col)
print("samlet_lager (internt):", [h.split(":",1)[1].strip() for h in inv_cols])
if FJ_FIELD: print("%s:" % FJ_FIELD, [h.split(":",1)[1].strip() for h in fj_cols])
if not sku_col or not inv_cols: sys.exit("Stop: mangler SKU- eller lager-kolonner.")
stock={}; fjern={}
for r in datarows:
    d=dict(zip(header,r)); k=(d.get(sku_col) or "").strip()
    if not k: continue
    stock[k]=stock.get(k,0)+sum(to_int(d.get(c)) for c in inv_cols)
    if FJ_FIELD: fjern[k]=fjern.get(k,0)+sum(to_int(d.get(c)) for c in fj_cols)
print("Laeste %d unikke SKU'er fra filen." % len(stock))
 
FULL=bool(OUTPUT_CSV)
auth()
plan=[]; skus=list(stock)
req_attrs=["sku","label","product_type",TARGET]+([FJ_FIELD] if FJ_FIELD else [])
 
def changed(pr,val,fj):
    cur=cur_val(pr,TARGET)
    if cur is None or to_int(cur)!=val: return True
    if FJ_FIELD:
        cf=cur_val(pr,FJ_FIELD)
        if cf is None or to_int(cf)!=fj: return True
    return False
 
# ---------- 2) Direkte SKU-match ----------
print("\nSlaar filens SKU'er op i Plytix...")
writes=0
for i in range(0,len(skus),40):
    batch=skus[i:i+40]
    st,b=call("POST",BASE+"/products/search",
              {"attributes":req_attrs,
               "filters":[[{"field":"sku","operator":"in","value":batch}]],
               "pagination":{"page":1,"page_size":100}})
    for pr in (b.get("data",[]) if isinstance(b,dict) else []):
        sku=(pr.get("sku") or "").strip(); val=stock.get(sku,0); fj=fjern.get(sku,0)
        if FULL or changed(pr,val,fj):
            plan.append((pr["id"], sku, pr.get("label") or sku, val, fj)); writes+=1
    print("  ...%d/%d SKU'er slaaet op, %d i planen" % (min(i+40,len(skus)),len(skus),writes))
 
# ---------- 3) PARENT: sum af egne boern ----------
print("\nHenter PARENT-produkter...")
parent_info={}; page=1; parents=0
while True:
    st,b=call("POST",BASE+"/products/search",
              {"attributes":["sku","label",TARGET]+([FJ_FIELD] if FJ_FIELD else []),
               "filters":[[{"field":"product_type","operator":"eq","value":"PARENT"}]],
               "pagination":{"page":page,"page_size":100}})
    data=b.get("data",[]) if isinstance(b,dict) else []
    if not data: break
    for pr in data:
        parents+=1; psku=(pr.get("sku") or "").strip()
        if psku: parent_info[psku]=pr
    page+=1
print("  gennemgik %d PARENT-produkter." % parents)
 
parent_skus=set(parent_info)
def find_parent(csku):
    s=csku
    while "_" in s:
        s=s.rsplit("_",1)[0]
        if s in parent_skus: return s
    return None
psum={}; pfj={}
for sku in stock:
    p=find_parent(sku)
    if p is not None:
        psum[p]=psum.get(p,0)+stock[sku]
        if FJ_FIELD: pfj[p]=pfj.get(p,0)+fjern.get(sku,0)
for psku,pr in parent_info.items():
    val=psum.get(psku,0); fj=pfj.get(psku,0)
    if FULL or changed(pr,val,fj):
        plan.append((pr["id"], psku, (pr.get("label") or psku)+" [PARENT]", val, fj))
 
# ---------- 4a) Import-fil ----------
if OUTPUT_CSV:
    cols=["SKU",TARGET]+([FJ_FIELD] if FJ_FIELD else [])
    with open(OUTPUT_CSV,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(cols)
        for pid,sku,label,val,fj in plan:
            if sku: w.writerow([sku,val]+([fj] if FJ_FIELD else []))
    print("\nSkrev import-fil: %s med %d raekker (%s)." % (OUTPUT_CSV,len(plan),", ".join(cols)))
    sys.exit(0)
 
# ---------- 4b) Plan / skrivning ----------
print("\n=== PLAN ===")
print("Produkter der skal opdateres:", len(plan))
for pid,sku,label,val,fj in plan[:15]:
    extra=("  %s=%s"%(FJ_FIELD,fj)) if FJ_FIELD else ""
    print("  %-38s %s=%s%s" % (str(label)[:38], TARGET, val, extra))
if len(plan)>15: print("  ... (+%d flere)" % (len(plan)-15))
 
if DRY:
    print("\nDRY_RUN=1: intet skrevet. Saet DRY_RUN=0 for at skrive rigtigt.")
    sys.exit(0)
 
print("\nSkriver til Plytix...")
ok=err=0
for i,(pid,sku,label,val,fj) in enumerate(plan,1):
    attrs={TARGET:val}
    if FJ_FIELD: attrs[FJ_FIELD]=fj
    st,b=call("PATCH",BASE+"/products/%s"%pid,{"attributes":attrs})
    if st in (200,201): ok+=1
    else:
        err+=1
        if err<=5: print("  FEJL",st,label,b)
    if i%50==0: print("  ...%d/%d (ok=%d fejl=%d)"%(i,len(plan),ok,err))
print("Faerdig. Skrevet ok=%d, fejl=%d." % (ok,err))
