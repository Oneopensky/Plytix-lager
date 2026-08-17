#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Byg Plytix import-fil og laeg den paa boksen.  INGEN per-produkt skrivning til Plytix.
 
Flow:
  1) Hent nyeste Matrixify-CSV fra boksen (SFTP_FILE-mappen)
  2) Beregn samlet_lager (interne lokationer) + fjernlager_antal (Fjernlager) pr. SKU
  3) Hent parent-SKU'er fra Plytix (product_type=PARENT) og summer deres egne boern (via SKU)
  4) Skriv import-CSV (SKU, samlet_lager, fjernlager_antal)
  5) Upload den til boksen (UPLOAD_PATH) - Plytix' egen import laeser den derfra
 
Miljovariabler:
  PLX_KEY, PLX_PWD            (kraevet - kun til at hente parent-listen, ingen skrivning)
  SFTP_HOST, SFTP_USER, SFTP_PASS
  SFTP_FILE=/Plytix/         mappe paa boksen med Matrixify-eksporten (nyeste .csv bruges)
  UPLOAD_PATH=/Plytix-import/plytix_lager.csv   hvor import-filen laegges paa boksen
  TARGET_FIELD=samlet_lager
  FJERNLAGER_FIELD=fjernlager_antal   (tomt = spring fjernlager over)
  FJERNLAGER_LOCATION=Fjernlager
  EXCLUDE_LOCATIONS=Business Central,Fjernlager   (udelades fra samlet_lager)
  LOCAL_ONLY=1               skriv kun lokalt (OUTPUT_LOCAL), upload ikke (til test)
  OUTPUT_LOCAL=lager_import.csv   lokalt filnavn ved test
"""
import os, json, sys, csv, time, tempfile, stat as _stat, urllib.request, urllib.error
 
KEY=os.environ.get("PLX_KEY","").strip(); PWD=os.environ.get("PLX_PWD","").strip()
SFTP_HOST=os.environ.get("SFTP_HOST","").strip()
SFTP_USER=os.environ.get("SFTP_USER","").strip()
SFTP_PASS=os.environ.get("SFTP_PASS","").strip()
SFTP_FILE=os.environ.get("SFTP_FILE","/Plytix/").strip()
UPLOAD_PATH=os.environ.get("UPLOAD_PATH","/Plytix-import/plytix_lager.csv").strip()
TARGET=os.environ.get("TARGET_FIELD","samlet_lager").strip()
FJ_FIELD=os.environ.get("FJERNLAGER_FIELD","").strip()
FJ_LOC=os.environ.get("FJERNLAGER_LOCATION","Fjernlager").strip().lower()
EXCL=[s.strip().lower() for s in os.environ.get("EXCLUDE_LOCATIONS","").split(",") if s.strip()]
LOCAL_ONLY=os.environ.get("LOCAL_ONLY","0")=="1"
OUTPUT_LOCAL=os.environ.get("OUTPUT_LOCAL","lager_import.csv").strip()
CSV_PATH=os.environ.get("CSV_PATH","").strip()   # valgfri lokal input-fil (test)
if not KEY or not PWD: sys.exit("Saet PLX_KEY og PLX_PWD.")
 
import paramiko
def sftp_open():
    t=paramiko.Transport((SFTP_HOST,22))
    t.connect(username=SFTP_USER,password=SFTP_PASS)
    return t, paramiko.SFTPClient.from_transport(t)
 
# ---------- 1) Hent input ----------
if CSV_PATH and os.path.exists(CSV_PATH):
    inp=CSV_PATH
else:
    t,s=sftp_open()
    d=SFTP_FILE if SFTP_FILE.endswith("/") else SFTP_FILE+"/"
    files=[e for e in s.listdir_attr(d) if e.filename.lower().endswith(".csv")]
    if not files: sys.exit("Ingen CSV i "+d)
    newest=max(files,key=lambda e:e.st_mtime)
    inp=os.path.join(tempfile.gettempdir(),"mtx_input.csv")
    s.get(d+newest.filename,inp); s.close(); t.close()
    print("Hentede input:",newest.filename)
 
# ---------- 2) Laes + beregn ----------
def read_matrixify(path):
    with open(path,"r",encoding="utf-8-sig",newline="") as f:
        rows=[r for r in csv.reader(f) if r]
    if rows and max(len(r) for r in rows[:5])==1:
        rows=[next(csv.reader([r[0]])) for r in rows]
    return (rows[0],rows[1:]) if rows else ([],[])
def to_int(x):
    s=str(x or "").strip().replace(" ","")
    if s in ("","-"): return 0
    try: return int(float(s.replace(",",".")))
    except Exception: return 0
 
header,data=read_matrixify(inp)
sku_col=next((h for h in header if h and h.strip().lower() in ("variant sku","sku")),None) \
        or next((h for h in header if h and "sku" in h.lower()),None)
all_inv=[h for h in header if h and h.strip().lower().startswith("inventory available")]
inv_cols=[h for h in all_inv if not any(w in h.lower() for w in EXCL)]
fj_cols=[h for h in all_inv if FJ_LOC in h.lower()] if FJ_FIELD else []
print("samlet_lager (internt):",[h.split(":",1)[1].strip() for h in inv_cols])
if FJ_FIELD: print("%s:"%FJ_FIELD,[h.split(":",1)[1].strip() for h in fj_cols])
if not sku_col or not inv_cols: sys.exit("Stop: mangler SKU- eller lager-kolonner.")
stock={}; fjern={}
for r in data:
    dct=dict(zip(header,r)); k=(dct.get(sku_col) or "").strip()
    if not k: continue
    stock[k]=stock.get(k,0)+sum(to_int(dct.get(c)) for c in inv_cols)
    if FJ_FIELD: fjern[k]=fjern.get(k,0)+sum(to_int(dct.get(c)) for c in fj_cols)
print("Beregnede %d SKU'er fra filen." % len(stock))
 
# ---------- 3) Parent-liste fra Plytix ----------
BASE="https://pim.plytix.com/api/v1"; _tok=[None]
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
    st,b=_raw("POST","https://auth.plytix.com/auth/api/get-token",{"api_key":KEY,"api_password":PWD},bearer=False)
    if st!=200: sys.exit("Auth fejl: %s"%b)
    _tok[0]=b["data"][0]["access_token"]
def call(m,u,p=None,_retry=True):
    st,b=_raw(m,u,p)
    if st==401 and _retry: auth(); return call(m,u,p,_retry=False)
    if st==429: time.sleep(2); return call(m,u,p,_retry=False)
    return st,b
auth()
print("Henter parent-SKU'er fra Plytix...")
parent_skus=set(); page=1
while True:
    st,b=call("POST",BASE+"/products/search",
              {"attributes":["sku"],
               "filters":[[{"field":"product_type","operator":"eq","value":"PARENT"}]],
               "pagination":{"page":page,"page_size":100}})
    dd=b.get("data",[]) if isinstance(b,dict) else []
    if not dd: break
    for pr in dd:
        s2=(pr.get("sku") or "").strip()
        if s2: parent_skus.add(s2)
    page+=1
print("  fandt",len(parent_skus),"parents.")
 
def find_parent(csku):
    s=csku
    while "_" in s:
        s=s.rsplit("_",1)[0]
        if s in parent_skus: return s
    return None
p_int={}; p_fj={}
for sku in stock:
    p=find_parent(sku)
    if p is not None:
        p_int[p]=p_int.get(p,0)+stock[sku]
        if FJ_FIELD: p_fj[p]=p_fj.get(p,0)+fjern.get(sku,0)
 
# ---------- 4) Skriv import-CSV ----------
out_local=os.path.join(tempfile.gettempdir(),"plytix_lager_out.csv")
cols=["SKU",TARGET]+([FJ_FIELD] if FJ_FIELD else [])
n=0
with open(out_local,"w",newline="",encoding="utf-8-sig") as f:
    w=csv.writer(f); w.writerow(cols)
    for sku in stock:
        w.writerow([sku,stock[sku]]+([fjern.get(sku,0)] if FJ_FIELD else [])); n+=1
    for p in parent_skus:
        w.writerow([p,p_int.get(p,0)]+([p_fj.get(p,0)] if FJ_FIELD else [])); n+=1
print("Skrev import-fil med %d raekker (%s)." % (n,", ".join(cols)))
 
# ---------- 5) Upload (eller gem lokalt ved test) ----------
if LOCAL_ONLY:
    import shutil; shutil.copy(out_local,OUTPUT_LOCAL)
    print("LOCAL_ONLY: gemte",OUTPUT_LOCAL,"- uploadede ikke.")
    sys.exit(0)
t,s=sftp_open()
# opret mappe hvis den mangler
d=os.path.dirname(UPLOAD_PATH) or "/"
try: s.stat(d)
except IOError:
    try: s.mkdir(d)
    except Exception as e: print("Kunne ikke oprette",d,":",e)
s.put(out_local,UPLOAD_PATH); s.close(); t.close()
print("Uploadede import-fil til boksen:",UPLOAD_PATH)
