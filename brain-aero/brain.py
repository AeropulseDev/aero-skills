#!/usr/bin/env python3
"""Small stdlib-only FTS index for manuals, handoffs and knowledge-base text."""
import argparse, hashlib, json, re, sqlite3, sys
from pathlib import Path
SCHEMA="""CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL, mtime REAL NOT NULL);CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,path TEXT NOT NULL,kind TEXT NOT NULL,heading TEXT NOT NULL,line_from INTEGER NOT NULL,line_to INTEGER NOT NULL,date TEXT,text TEXT NOT NULL,stem TEXT NOT NULL);CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(heading,stem,content='chunks',content_rowid='id',tokenize='unicode61');"""
DATE_RE=re.compile(r"(20\d{2}-\d{2}-\d{2})"); WORD_RE=re.compile(r"[\w][\w_-]*",re.UNICODE); ENDINGS=("иями","ами","ого","ему","ому","ими","ыми","ов","ев","ей","ам","ям","ах","ях","ы","и","а","я","о","е","у","ю")
def normalize_word(w):
 w=w.lower().replace("ё","е")
 if len(w)>=5 and not ("_" in w or any(c.isdigit() for c in w) or re.search(r"[a-z]",w)):
  for e in ENDINGS:
   if w.endswith(e) and len(w)-len(e)>=4:return w[:-len(e)]
 return w
def stem_text(t):return " ".join(normalize_word(x) for x in WORD_RE.findall(t))
def read_utf8(p):
 try:return p.read_text(encoding="utf-8").replace("\r\n","\n").replace("\r","\n")
 except UnicodeDecodeError as e:raise ValueError("UTF-8 decode failed for %s: %s"%(p,e))
def date_for(p,t):
 m=DATE_RE.search(p.name) or DATE_RE.search(t[:500]);return m.group(1) if m else None
def markdown_chunks(p,kind):
 t=read_utf8(p); lines=t.splitlines(); heads=[]; fenced=False
 for i,line in enumerate(lines):
  if re.match(r"^\s*(```|~~~)",line):fenced=not fenced;continue
  m=re.match(r"^(#{1,6})\s+(.+?)\s*$",line)
  if m and not fenced:heads.append((i,m.group(2).strip()))
 out=[]
 if heads:
  for n,(s,h) in enumerate(heads):
   e=heads[n+1][0] if n+1<len(heads) else len(lines); body="\n".join(lines[s:e]).strip()
   if body:out.append((h,s+1,e,date_for(p,t),body))
 elif t.strip():out.append((p.stem,1,len(lines),date_for(p,t),t.strip()))
 return out
def kb_chunks(p):
 t=read_utf8(p)
 if p.suffix.lower() in (".md",".markdown"):return markdown_chunks(p,"kb")
 return [(p.stem,i+1,i+1,date_for(p,t),line.strip()) for i,line in enumerate(t.splitlines()) if line.strip()]
def config_load(path):
 p=Path(path).resolve();d=json.loads(p.read_text(encoding="utf-8"));d["_dir"]=p.parent;return d
def source_files(c):
 for s in c.get("sources",[]):
  r=Path(s["root"]);r=r if r.is_absolute() else c["_dir"]/r
  if not r.exists():print("warning: source missing: %s"%r,file=sys.stderr);continue
  for p in r.glob(s.get("glob","**/*")):
   if p.is_file():yield s["kind"],r,p
def parse_file(k,p):return kb_chunks(p) if k=="kb" else markdown_chunks(p,k)
def db_path(c):
 p=Path(c.get("database","brain.db"));return p if p.is_absolute() else c["_dir"]/p
def build(c,full=False,verbose=False):
 dbp=db_path(c);dbp.parent.mkdir(parents=True,exist_ok=True);con=sqlite3.connect(str(dbp));stats={"indexed":0,"unchanged":0,"removed":0,"errors":0}
 try:
  con.executescript(SCHEMA);con.execute("BEGIN");seen={}
  for k,r,p in source_files(c):
   key="%s:%s"%(k,p.resolve());raw=p.read_bytes();sha=hashlib.sha256(raw).hexdigest();mt=p.stat().st_mtime;seen[key]=(k,sha,mt);old=con.execute("SELECT sha,mtime FROM files WHERE path=?",(key,)).fetchone()
   if not full and old and old[0]==sha and old[1]==mt:stats["unchanged"]+=1;continue
   try:chunks=parse_file(k,p)
   except (OSError,ValueError) as e:print("warning: %s"%e,file=sys.stderr);stats["errors"]+=1;continue
   con.execute("DELETE FROM chunks WHERE path=? AND kind=?",(key,k));con.execute("DELETE FROM files WHERE path=? AND kind=?",(key,k));con.execute("INSERT INTO files VALUES(?,?,?,?)",(key,k,sha,mt))
   for h,a,b,d,t in chunks:con.execute("INSERT INTO chunks(path,kind,heading,line_from,line_to,date,text,stem) VALUES(?,?,?,?,?,?,?,?)",(key,k,h,a,b,d,t,stem_text(h+" "+t)))
   stats["indexed"]+=1
  for p,k in con.execute("SELECT path,kind FROM files").fetchall():
   if p not in seen:con.execute("DELETE FROM chunks WHERE path=? AND kind=?",(p,k));con.execute("DELETE FROM files WHERE path=? AND kind=?",(p,k));stats["removed"]+=1
  con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')");con.commit()
 except Exception:
  con.rollback();raise
 finally:con.close()
 if verbose:print("indexed={indexed} unchanged={unchanged} removed={removed} errors={errors}".format(**stats))
 return stats
def fts_query(q,syn):
 w=[normalize_word(x) for x in WORD_RE.findall(q)];e=list(w)
 for x in w:e += [normalize_word(a) for a in syn.get(x,[])]
 return " OR ".join('"%s"'%x.replace('"','""') for x in dict.fromkeys(e) if x) or '""'
def find(c,q,kinds=None,since=None,limit=8):
 con=sqlite3.connect(str(db_path(c)))
 try:
  sql="SELECT c.kind,c.path,c.line_from,c.line_to,c.heading,c.text,bm25(chunks_fts,3.0,1.0),c.date FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ?";a=[fts_query(q,{normalize_word(k):v for k,v in c.get("synonyms",{}).items()})]
  if kinds:sql+=" AND c.kind IN (%s)"%",".join("?"*len(kinds));a+=list(kinds)
  if since:sql+=" AND (c.date IS NULL OR c.date>=?)";a.append(since)
  sql += " ORDER BY 7,c.kind,c.path,c.line_from LIMIT ?"
  rows=con.execute(sql,a+[limit]).fetchall()
 finally:con.close()
 for k,p,x,y,h,t,_,_ in rows:print("%s | %s:%s-%s | %s | %s"%(k,p,x,y,h," ".join(t.split())[:240]))
 return rows
def stats(c):
 p=db_path(c)
 if not p.exists():print("files=0 chunks=0");return
 con=sqlite3.connect(str(p));f=con.execute("SELECT count(*) FROM files").fetchone()[0];n=con.execute("SELECT count(*) FROM chunks").fetchone()[0];con.close();print("files=%d chunks=%d"%(f,n))
def main(argv=None):
 a=argparse.ArgumentParser();s=a.add_subparsers(dest="cmd",required=True)
 for n in("build","stats"):
  p=s.add_parser(n);p.add_argument("--config",default="brain.config.json")
  if n=="build":p.add_argument("--full",action="store_true");p.add_argument("--verbose",action="store_true")
 p=s.add_parser("find");p.add_argument("query");p.add_argument("--config",default="brain.config.json");p.add_argument("--kind",action="append");p.add_argument("--since");p.add_argument("-n",type=int,default=8)
 x=a.parse_args(argv);c=config_load(x.config)
 if x.cmd=="build":build(c,x.full,x.verbose)
 elif x.cmd=="stats":stats(c)
 else:find(c,x.query,x.kind,x.since,x.n)
if __name__=="__main__":main()
