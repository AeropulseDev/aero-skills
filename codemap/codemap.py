#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codemap — карта кода проекта: файлы → классы, функции, роуты, хендлеры.

Только стандартная библиотека; код не импортируется и не исполняется.
Читает только файлы по расширению (.py .gs .js .html) — .env, *.json, ключи не открываются никогда.

  python3 codemap.py <root> [<root2> ...] [--out X.md] [--json X.json] [--overview X.md] ...
  python3 codemap.py diff a.json b.json [--out diff.md]

Подробно — README.md и SKILL.md рядом."""
from __future__ import annotations

import argparse
import ast
import datetime
import fnmatch
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

VERSION = "2026-10-20"
SCHEMA = 1

EXT = {".py": "py", ".gs": "gs", ".js": "js", ".mjs": "js", ".html": "html", ".htm": "html"}
SKIP_DIRS = {"venv", "env", "__pycache__", "node_modules", "site-packages", "dist", "build",
             "secrets", "tmp", "tmp_data"}
SKIP_SUFFIX = ("alembic/versions",)
MAX_BYTES = 2_000_000
FILE_DOC_MAX = 400

HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options", "websocket"}
AIOHTTP_ADD = {"add_get": "GET", "add_post": "POST", "add_put": "PUT", "add_delete": "DELETE",
               "add_patch": "PATCH", "add_head": "HEAD"}
BOT_EVENTS = {"message", "callback_query", "inline_query", "chosen_inline_result", "edited_message",
              "channel_post", "edited_channel_post", "my_chat_member", "chat_member", "poll",
              "poll_answer", "pre_checkout_query", "shipping_query", "chat_join_request",
              "business_message", "edited_business_message", "message_reaction"}
GAS_TRIGGERS = {"doGet", "doPost", "onOpen", "onEdit", "onInstall", "onSelectionChange", "onFormSubmit"}
GAS_RUN_SKIP = {"withSuccessHandler", "withFailureHandler", "withUserObject"}


# ───────────────────────────── общие помощники ─────────────────────────────

def collapse(s: str) -> str:
    return " ".join((s or "").split())


FULL_DOC = False  # --full-doc: весь докстринг, а не первый абзац


def first_para(doc: str | None, limit: int = 1000) -> str:
    if not doc:
        return ""
    para = doc.strip() if FULL_DOC else doc.strip().split("\n\n")[0]
    limit = max(limit, 2000) if FULL_DOC else limit
    s = collapse(para)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def cut(s: str, n: int) -> str:
    s = collapse(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def unparse(n) -> str:
    try:
        return ast.unparse(n)
    except Exception:
        return "..."


def const_str(n):
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def deps_in(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?:Depends|Security)\(\s*([\w\.]+)", text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def iter_stmts(body):
    for st in body:
        yield st
        if isinstance(st, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
            for field in ("body", "orelse", "finalbody"):
                yield from iter_stmts(getattr(st, field, None) or [])
            for h in getattr(st, "handlers", None) or []:
                yield from iter_stmts(h.body)


# ───────────────────────────── Python ─────────────────────────────

class PyCollector:
    def __init__(self, tree: ast.Module, nested: bool):
        self.tree = tree
        self.nested = nested
        self.items: list[dict] = []
        self.prefixes: dict[str, str] = {}
        self.router_deps: dict[str, list[str]] = {}
        self.includes: list[dict] = []
        self.regs: list[dict] = []      # router.message.register(fn, ...)
        self.ext_routes: list[dict] = []  # app.router.add_get(path, fn), web.get(path, fn)
        self._prescan()

    # --- предварительный проход по всему дереву
    def _prescan(self):
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                v = n.value
                if isinstance(v, ast.Call) and unparse(v.func).endswith("APIRouter"):
                    pref = next((const_str(k.value) for k in v.keywords if k.arg == "prefix"), None)
                    rdeps = [d for k in v.keywords if k.arg == "dependencies" for d in deps_in(unparse(k.value))]
                    tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in tg:
                        if pref:
                            self.prefixes[unparse(t)] = pref
                        if rdeps:
                            self.router_deps[unparse(t)] = rdeps
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            attr = n.func.attr
            if attr == "include_router" and n.args:
                pref = next((const_str(k.value) for k in n.keywords if k.arg == "prefix"), None)
                idep = [d for k in n.keywords if k.arg == "dependencies" for d in deps_in(unparse(k.value))]
                self.includes.append({"target": cut(unparse(n.args[0]), 60), "prefix": pref or "",
                                      "deps": idep, "line": n.lineno})
            elif attr == "register" and isinstance(n.func.value, ast.Attribute) \
                    and n.func.value.attr in BOT_EVENTS and n.args:
                fn = unparse(n.args[0]).split(".")[-1]
                self.regs.append({"func": fn, "event": n.func.value.attr, "filters": self._filters(n.args[1:], n.keywords),
                                  "owner": unparse(n.func.value.value)})
            elif attr in AIOHTTP_ADD and len(n.args) >= 2 and const_str(n.args[0]) is not None:
                self.ext_routes.append({"method": AIOHTTP_ADD[attr], "path": const_str(n.args[0]),
                                        "func": unparse(n.args[1]).split(".")[-1], "owner": unparse(n.func.value),
                                        "line": n.lineno, "deps": []})
            elif attr == "add_route" and len(n.args) >= 3 and const_str(n.args[0]) and const_str(n.args[1]):
                self.ext_routes.append({"method": const_str(n.args[0]).upper(), "path": const_str(n.args[1]),
                                        "func": unparse(n.args[2]).split(".")[-1], "owner": unparse(n.func.value),
                                        "line": n.lineno, "deps": []})
            elif attr == "add_api_route" and len(n.args) >= 2 and const_str(n.args[0]) is not None:
                meth = next((",".join(const_str(e) or "" for e in k.value.elts).upper()
                             for k in n.keywords if k.arg == "methods" and isinstance(k.value, (ast.List, ast.Tuple, ast.Set))), "GET")
                self.ext_routes.append({"method": meth, "path": const_str(n.args[0]),
                                        "func": unparse(n.args[1]).split(".")[-1], "owner": unparse(n.func.value),
                                        "line": n.lineno, "deps": []})
            elif attr in HTTP_VERBS and isinstance(n.func.value, ast.Name) and n.func.value.id == "web" \
                    and len(n.args) >= 2 and const_str(n.args[0]) is not None:
                self.ext_routes.append({"method": attr.upper(), "path": const_str(n.args[0]),
                                        "func": unparse(n.args[1]).split(".")[-1], "owner": "web",
                                        "line": n.lineno, "deps": []})

    @staticmethod
    def _filters(args, kws) -> str:
        parts = [unparse(a) for a in args]
        for k in kws:
            parts.append(f"{k.arg}={unparse(k.value)}" if k.arg else "**" + unparse(k.value))
        return cut(", ".join(parts), 160)

    # --- декораторы
    def _route(self, d):
        if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
            return None
        verb = d.func.attr
        path = None
        if verb in HTTP_VERBS or verb == "api_route":
            if d.args:
                path = const_str(d.args[0])
            if path is None:
                path = next((const_str(k.value) for k in d.keywords if k.arg == "path"), None)
        if path is None:
            return None
        if verb == "api_route":
            method = next((",".join(const_str(e) or "" for e in k.value.elts).upper() for k in d.keywords
                           if k.arg == "methods" and isinstance(k.value, (ast.List, ast.Tuple, ast.Set))), "ANY")
        else:
            method = verb.upper()
        owner = unparse(d.func.value)
        deps = []
        for k in d.keywords:
            if k.arg == "dependencies":
                deps += deps_in(unparse(k.value))
        return {"method": method, "path": path, "owner": owner, "deps": deps}

    def _handler(self, d):
        node = d.func if isinstance(d, ast.Call) else d
        if not isinstance(node, ast.Attribute):
            return None
        ev = node.attr
        if ev in BOT_EVENTS or (ev.endswith("_handler") and ev != "error_handler"):
            args = d.args if isinstance(d, ast.Call) else []
            kws = d.keywords if isinstance(d, ast.Call) else []
            return {"event": ev, "filters": self._filters(args, kws), "owner": unparse(node.value)}
        return None

    def _param_deps(self, n) -> list[str]:
        out = []
        defaults = list(n.args.defaults) + [x for x in n.args.kw_defaults if x is not None]
        for x in defaults:
            out += deps_in(unparse(x))
        for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs:
            if a.annotation is not None:
                out += deps_in(unparse(a.annotation))
        return list(OrderedDict.fromkeys(out))

    # --- обход
    def run(self):
        self._body(self.tree.body, "", False, 0)
        self._attach_external()
        self.items.sort(key=lambda x: x["line"])
        return self.items

    def _body(self, body, prefix, in_class, fdepth):
        # fdepth — сколько функций выше по дереву; вложенное пишем в карту только с --nested
        # или если это роут/хендлер
        for st in iter_stmts(body):
            if isinstance(st, ast.ClassDef):
                if fdepth == 0 or self.nested:
                    self.items.append(self._class_item(st, prefix))
                self._body(st.body, prefix + st.name + ".", True, fdepth)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                it = self._func_item(st, prefix, in_class)
                if fdepth == 0 or self.nested or it.get("routes") or it.get("handlers"):
                    self.items.append(it)
                self._body(st.body, prefix + st.name + ".", False, fdepth + 1)

    def _class_item(self, c: ast.ClassDef, prefix) -> dict:
        bases = ", ".join(unparse(b) for b in c.bases)
        it = {"kind": "class", "name": prefix + c.name, "line": c.lineno, "args": bases, "returns": "",
              "async": False, "decorators": [unparse(d) for d in c.decorator_list],
              "doc": first_para(ast.get_docstring(c))}
        if any(unparse(b).endswith("StatesGroup") for b in c.bases):
            states = []
            for st in c.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Call) \
                        and unparse(st.value.func).split(".")[-1] == "State":
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    states += [unparse(t) for t in tg]
            it["states"] = states
        return it

    def _func_item(self, f, prefix, in_class) -> dict:
        decos, routes, handlers = [], [], []
        pdeps = self._param_deps(f)
        for d in f.decorator_list:
            decos.append(unparse(d))
            r = self._route(d)
            if r:
                pref = self.prefixes.get(r["owner"], "")
                r["full"] = (pref + r["path"]) if pref else r["path"]
                r["deps"] = list(OrderedDict.fromkeys(self.router_deps.get(r["owner"], []) + r["deps"] + pdeps))
                routes.append(r)
            h = self._handler(d)
            if h:
                handlers.append(h)
        it = {"kind": "method" if in_class else "function", "name": prefix + f.name, "line": f.lineno,
              "args": unparse(f.args), "returns": unparse(f.returns) if f.returns is not None else "",
              "async": isinstance(f, ast.AsyncFunctionDef), "decorators": decos,
              "doc": first_para(ast.get_docstring(f))}
        if routes:
            it["routes"] = routes
        if handlers:
            it["handlers"] = handlers
        return it

    def _attach_external(self):
        def find(fn):
            for it in self.items:
                if it["kind"] != "class" and (it["name"] == fn or it["name"].endswith("." + fn)):
                    return it
            return None
        for r in self.regs:
            it = find(r["func"])
            if it is not None:
                it.setdefault("handlers", []).append({"event": r["event"], "filters": r["filters"], "owner": r["owner"]})
        for r in self.ext_routes:
            it = find(r["func"])
            route = {"method": r["method"], "path": r["path"], "full": r["path"], "owner": r["owner"], "deps": []}
            if it is None:
                it = {"kind": "function", "name": r["func"], "line": r["line"], "args": "?", "returns": "",
                      "async": False, "decorators": [], "doc": "", "external": True}
                self.items.append(it)
            it.setdefault("routes", []).append(route)


def file_comment(text: str) -> str:
    for ln in text.splitlines()[:15]:
        s = ln.strip()
        if not s:
            continue
        if not s.startswith("#"):
            break
        if s.startswith("#!") or re.search(r"coding[:=]", s):
            continue
        t = s.lstrip("# ").strip()
        if not t or re.fullmatch(r"[-─═=*_~#\s]+", t):
            continue
        return t
    return ""


def parse_py(text: str, nested: bool) -> dict:
    tree = ast.parse(text)
    col = PyCollector(tree, nested)
    items = col.run()
    consts = []
    for st in iter_stmts(tree.body):
        if isinstance(st, ast.Assign):
            consts += [t.id for t in st.targets if isinstance(t, ast.Name) and t.id.isupper()]
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id.isupper():
            consts.append(st.target.id)
    doc = first_para(ast.get_docstring(tree)) or file_comment(text)
    return {"doc": doc, "consts": list(OrderedDict.fromkeys(consts)), "items": items,
            "includes": col.includes}


# ───────────────────────────── GAS / JS / HTML ─────────────────────────────

def js_regexes(indent):
    ind = ("[ \\t]{%d}" % indent) if indent else ""
    fn = re.compile(r"^" + ind + r"(?:export\s+)?((?:async\s+)?)function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", re.M)
    arrow = re.compile(r"^" + ind + r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                       r"((?:async\s+)?)(?:function\s*\*?\s*[\w$]*\s*\(([^)]*)\)|\(([^)]*)\)\s*=>|([A-Za-z_$][\w$]*)\s*=>)", re.M)
    const = re.compile(r"^" + ind + r"(?:const|var|let)\s+([A-Z][A-Z0-9_]{2,})\s*=", re.M)
    return fn, arrow, const


def js_comment(lines: list[str], idx: int) -> str:
    """Комментарий строго над функцией: подряд идущие `//` или один блок `/* ... */` (в т. ч. JSDoc)."""
    out, j = [], idx - 1
    while j >= 0 and len(out) < 40:
        s = lines[j].strip()
        if s.endswith("*/"):
            blk, k = [s], j
            while "/*" not in blk[-1] and k > 0:
                k -= 1
                blk.append(lines[k].strip())
            out = blk[::-1] + out
            j = k - 1
        elif s.startswith("//"):
            out.insert(0, s)
            j -= 1
        else:
            break
    clean = []
    for s in out:
        s = re.sub(r"^(/\*+|\*+/|\*+|//+)\s?", "", s)
        s = re.sub(r"\*+/\s*$", "", s).strip()
        if s and not s.startswith("@") and not re.fullmatch(r"[-─═=*_~\s]+", s) \
                and not re.fullmatch(r"[─═\-=\s]+.{0,60}[─═\-=\s]{4,}", s):
            clean.append(s)
    return cut(" ".join(clean), 300)


def parse_js_block(text: str, indent: int, offset: int = 0):
    fn_re, arrow_re, const_re = js_regexes(indent)
    lines = text.splitlines()
    items = []

    def add(name, asyn, args, m_start):
        idx = text.count("\n", 0, m_start)
        it = {"kind": "function", "name": name, "line": idx + 1 + offset, "args": collapse(args), "returns": "",
              "async": bool(asyn.strip()), "decorators": ["GAS trigger"] if name in GAS_TRIGGERS else [],
              "doc": js_comment(lines, idx)}
        items.append(it)

    for m in fn_re.finditer(text):
        add(m.group(2), m.group(1), m.group(3), m.start())
    for m in arrow_re.finditer(text):
        args = m.group(3) if m.group(3) is not None else (m.group(4) if m.group(4) is not None else m.group(5) or "")
        add(m.group(1), m.group(2), args, m.start())
    consts = [m.group(1) for m in const_re.finditer(text)]
    return items, consts


def gas_run_calls(text: str) -> list[str]:
    names = set()
    for m in re.finditer(r"google\.script\.run", text):
        i = m.end()
        while True:
            mm = re.match(r"\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", text[i:i + 200])
            if not mm:
                break
            name = mm.group(1)
            j = i + mm.end()
            depth = 1
            while j < len(text) and depth:
                c = text[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            if name not in GAS_RUN_SKIP:
                names.add(name)
                break
            i = j
    return sorted(names)


def url_strings(text: str) -> list[str]:
    urls = set(re.findall(r"['\"`](/[a-z_/]+)['\"`]", text)) | set(re.findall(r"apiPath\(?\s*['\"]([^'\"]+)", text))
    return sorted(u for u in urls if len(u) > 2)


def parse_js(text: str, lang: str) -> dict:
    items, consts, doc = [], [], ""
    if lang == "html":
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        doc = collapse(m.group(1)) if m else ""
        for sm in re.finditer(r"<script\b([^>]*)>(.*?)</script>", text, re.S | re.I):
            if re.search(r"\bsrc\s*=", sm.group(1)):
                continue
            off = text.count("\n", 0, sm.start(2))
            body = sm.group(2)
            nonempty = [ln for ln in body.splitlines() if ln.strip()]
            base = min((len(ln) - len(ln.lstrip(" \t")) for ln in nonempty), default=0)
            it, cs = parse_js_block(body, base, off)
            items += it
            consts += cs
    else:
        items, consts = parse_js_block(text, 0)
        for ln in text.splitlines()[:6]:
            s = ln.strip()
            if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
                t = re.sub(r"^(/\*+|\*+|//+)\s?", "", s).strip(" */")
                if t and not re.fullmatch(r"[-=*_\s]+", t):
                    doc = t
                    break
            elif s:
                break
    items.sort(key=lambda x: x["line"])
    out = {"doc": doc, "consts": list(OrderedDict.fromkeys(consts)), "items": items, "includes": []}
    calls = gas_run_calls(text)
    if calls:
        out["gas_calls"] = calls
    urls = url_strings(text)
    if urls:
        out["urls"] = urls
    return out


# ───────────────────────────── сбор файлов ─────────────────────────────

def norm_root(r: str) -> str:
    r = r.replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    return r.rstrip("/") or "."


def match_pat(rel: str, pat: str) -> bool:
    pat = pat.replace("\\", "/").rstrip("/")
    name = rel.rsplit("/", 1)[-1]
    return (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) or rel.startswith(pat + "/")
            or any(fnmatch.fnmatch(part, pat) for part in rel.split("/")[:-1]))


def skip_dir(name: str, rel_dir: str) -> bool:
    if name.startswith(".") or name in SKIP_DIRS or name.endswith(".egg-info") or name.endswith(".dist-info"):
        return True
    full = (rel_dir + "/" + name).strip("/")
    return any(full.endswith(s) for s in SKIP_SUFFIX)


def collect(roots: list[str], args) -> list[dict]:
    langs = {x.strip() for x in args.lang.split(",") if x.strip()}
    multi = len(roots) > 1
    files = []
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            sys.exit(f"codemap: нет папки {root}")
        label = norm_root(root)
        for dirpath, dirnames, filenames in os.walk(rp):
            rel_dir = Path(dirpath).relative_to(rp).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            d = 0 if not rel_dir else rel_dir.count("/") + 1
            if args.depth and d >= args.depth:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(x for x in dirnames if not skip_dir(x, rel_dir))
            for fname in sorted(filenames):
                ext = Path(fname).suffix.lower()
                lang = EXT.get(ext)
                if lang is None or lang not in langs:
                    continue
                rel = (rel_dir + "/" + fname) if rel_dir else fname
                if args.include and not any(match_pat(rel, p) for p in args.include):
                    continue
                if args.exclude and any(match_pat(rel, p) for p in args.exclude):
                    continue
                fp = Path(dirpath) / fname
                try:
                    if fp.stat().st_size > MAX_BYTES:
                        continue
                except OSError:
                    continue
                files.append({"path": (label + "/" + rel) if multi else rel, "rel": rel, "root": label,
                              "lang": lang, "_fp": fp})
    files.sort(key=lambda f: (roots.index(next(r for r in roots if norm_root(r) == f["root"])),
                              Path(f["rel"]).parts))
    return files


def analyze(files: list[dict], args) -> list[dict]:
    out = []
    for f in files:
        fp = f.pop("_fp")
        try:
            text = fp.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            out.append({**f, "lines": 0, "doc": "", "consts": [], "items": [], "includes": [], "error": str(e)})
            continue
        base = {**f, "lines": text.count("\n") + 1}
        try:
            data = parse_py(text, args.nested) if f["lang"] == "py" else parse_js(text, f["lang"])
            out.append({**base, **data})
        except SyntaxError as e:
            out.append({**base, "doc": "", "consts": [], "items": [], "includes": [],
                        "error": f"SyntaxError: {e.msg} (стр. {e.lineno})"})
    return out


# ───────────────────────────── sidecar ─────────────────────────────

class Sidecar:
    SECTIONS = {"о чём": "about", "about": "about", "вступление": "intro", "intro": "intro", "файлы": "files", "files": "files",
                "прочее": "outro", "outro": "outro"}

    def __init__(self, path: Path | None):
        self.intro, self.outro, self.about = "", "", ""
        self.files: "OrderedDict[str, str]" = OrderedDict()
        self.path = path
        if path and path.is_file():
            self._parse(path.read_text(encoding="utf-8-sig"))

    def _parse(self, text: str):
        has = any(re.match(r"^##\s+(.+?)\s*$", ln) and re.match(r"^##\s+(.+?)\s*$", ln).group(1).strip().lower() in self.SECTIONS
                  for ln in text.splitlines())
        sec, buf = (None if has else "files"), {"about": [], "intro": [], "outro": []}
        last = None
        for raw in text.splitlines():
            m = re.match(r"^##\s+(.+?)\s*$", raw)
            if m and m.group(1).strip().lower() in self.SECTIONS:
                sec, last = self.SECTIONS[m.group(1).strip().lower()], None
                continue
            if sec in buf:
                buf[sec].append(raw)
                continue
            if sec != "files" or raw.startswith("#"):
                continue
            mm = re.match(r"^\s*[-*]?\s*`?([\w./\\@\-\[\]*?]+)`?\s*:\s+(.*\S)\s*$", raw)
            if mm and not raw.startswith("  "):
                last = mm.group(1).replace("\\", "/")
                self.files[last] = mm.group(2)
            elif last and raw.startswith("  ") and raw.strip():
                self.files[last] += " " + raw.strip()
        self.about = collapse(" ".join(buf["about"]))
        self.intro = "\n".join(buf["intro"]).strip("\n")
        self.outro = "\n".join(buf["outro"]).strip("\n")

    def lookup(self, f: dict) -> str:
        for key in (f["path"], f["rel"]):
            if key in self.files:
                return self.files[key]
        for key, val in self.files.items():
            if any(c in key for c in "*?[") and (fnmatch.fnmatch(f["path"], key) or fnmatch.fnmatch(f["rel"], key)):
                return val
        return ""

    def order(self, f: dict) -> int | None:
        keys = list(self.files)
        for key in (f["path"], f["rel"]):
            if key in self.files:
                return keys.index(key)
        return None


# ───────────────────────────── вывод: Markdown ─────────────────────────────

def q(a: str) -> str:
    """Аргумент командной строки в двойных кавычках (работает и в Windows cmd/PowerShell, и в bash)."""
    return '"' + a.replace('"', '\\"') + '"' if re.search(r"[\s&|<>()^;$`']", a) else a


def regen_line(args) -> str:
    """Команда пересборки: из текущей папки, с путём к codemap.py относительно неё."""
    if args.regen:
        return f"Регенерация: `{args.regen}`"
    try:
        script = Path(os.path.relpath(Path(__file__).resolve(), Path.cwd())).as_posix()
    except ValueError:
        script = Path(__file__).resolve().as_posix()
    cmd = "python3 " + script + " " + " ".join(q(a) for a in sys.argv[1:])
    return f"Регенерация (из `{Path.cwd().name}/`): `{cmd}`"


def display(it: dict, max_sig: int) -> str:
    if it["kind"] == "class":
        return f"class {it['name']}" + (f"({cut(it['args'], max_sig)})" if it["args"] else "")
    a = it["args"]
    a = a if len(a) <= max_sig else a[: max_sig - 3] + "..."
    r = f" -> {cut(it['returns'], 40)}" if it.get("returns") else ""
    return f"{'async ' if it.get('async') else ''}{it['name']}({a}){r}"


def deco_text(it: dict) -> str:
    return " ".join(f"`@{cut(d, 48)}`" for d in it.get("decorators", []))


def item_doc(it: dict, max_doc: int) -> str:
    d = cut(it.get("doc", ""), max_doc)
    if it.get("states"):
        st = "Состояния: " + ", ".join(it["states"])
        d = (d + " " + st).strip()
    return esc(d)


def file_desc(f: dict, sc: Sidecar) -> str:
    return sc.lookup(f) or f.get("doc", "")


def render_md(files, args, sc: Sidecar, roots: list[str], name: str) -> str:
    today = datetime.date.today().isoformat()
    title = args.title or f"{name} — карта функций по файлам"
    about = args.about or sc.about or (f"Карта функций, классов, роутов и хендлеров по файлам ({', '.join('`' + norm_root(r) + '/`' for r in roots)}); "
                           f"сгенерирована `codemap` по AST, номера строк актуальны на момент генерации.")
    src = ", ".join(f"`{norm_root(r)}/`" for r in roots)
    out = [f"# {title}", "", cut(about, 600), "", f"Обновлено: {today} · Источник: {src} · {regen_line(args)}", ""]
    if sc.intro:
        out += [sc.intro, ""]

    routes, handlers, fsm = [], [], []
    for f in files:
        for it in f["items"]:
            for r in it.get("routes", []):
                routes.append((f, it, r))
            for h in it.get("handlers", []):
                handlers.append((f, it, h))
            if it.get("states") is not None:
                fsm.append((f, it))
    if routes:
        out += ["## Роуты HTTP", "", "| Метод | Путь | Функция | Файл:стр. | Deps | Что делает |", "|---|---|---|---|---|---|"]
        for f, it, r in routes:
            out.append(f"| {r['method']} | `{esc(r['full'])}` | `{esc(it['name'])}` | {esc(f['path'])}:{it['line']} | "
                       f"{esc(', '.join(r['deps'])) or '—'} | {item_doc(it, args.max_doc)} |")
        out.append("")
    if handlers:
        out += ["## Хендлеры бота", "", "| Событие | Фильтры | Функция | Файл:стр. | Что делает |", "|---|---|---|---|---|"]
        for f, it, h in handlers:
            out.append(f"| {h['event']} | {('`' + esc(h['filters']) + '`') if h['filters'] else '—'} | `{esc(it['name'])}` | "
                       f"{esc(f['path'])}:{it['line']} | {item_doc(it, args.max_doc)} |")
        out.append("")
    if fsm:
        out += ["## Состояния FSM", ""]
        for f, it in fsm:
            out.append(f"- `{it['name']}` ({f['path']}:{it['line']}): " + (", ".join(f"`{s}`" for s in it["states"]) or "—"))
        out.append("")

    multi = len(roots) > 1
    for root in roots:
        rl = norm_root(root)
        grp = [f for f in files if f["root"] == rl]
        out += [f"## Файлы и функции — `{rl}/`" if multi else "## Файлы и функции", ""]
        for f in grp:
            desc = file_desc(f, sc)
            has = f["items"] or f["consts"] or desc or f.get("error") or f.get("includes") or f.get("gas_calls")
            if not has:
                continue
            out += [f"### `{f['rel'] if multi else f['path']}`", ""]
            if f.get("error"):
                out += [f"Ошибка разбора: {f['error']}", ""]
            if desc:
                out += [desc if sc.lookup(f) else cut(desc, FILE_DOC_MAX), ""]
            if f["consts"]:
                out += ["Константы: " + ", ".join(f"`{c}`" for c in f["consts"][:25]) + (" …" if len(f["consts"]) > 25 else ""), ""]
            if f.get("includes"):
                out += ["Подключает роутеры: " + ", ".join(
                    f"`{i['target']}`" + (" (" + ", ".join(
                        ([f"prefix `{i['prefix']}`"] if i["prefix"] else []) + ([f"deps `{', '.join(i['deps'])}`"] if i.get("deps") else [])) + ")"
                        if (i["prefix"] or i.get("deps")) else "") for i in f["includes"]), ""]
            if f["items"]:
                out += ["| Стр. | Функция | Что делает |", "|---|---|---|"]
                for it in f["items"]:
                    dec = deco_text(it)
                    out.append(f"| {it['line']} | `{esc(display(it, args.max_sig))}`{(' ' + esc(dec)) if dec else ''} | {item_doc(it, args.max_doc)} |")
                out.append("")
            if f.get("gas_calls"):
                out += ["Вызовы сервера из HTML (`google.script.run`): " + ", ".join(f"`{c}`" for c in f["gas_calls"]), ""]
            if f.get("urls"):
                out += ["Строки-пути (возможные обращения к API): " + ", ".join(f"`{u}`" for u in f["urls"][:30]), ""]
    if sc.outro:
        out += [sc.outro, ""]
    return "\n".join(out).rstrip("\n") + "\n"


def render_overview(files, args, sc: Sidecar, roots, name) -> str:
    today = datetime.date.today().isoformat()
    src = ", ".join(f"`{norm_root(r)}/`" for r in roots)
    out = [f"# {args.overview_title or name + ' — обзор файлов'}", "",
           f"Файлы {src}: размер, число функций и назначение.", "",
           f"Обновлено: {today} · Источник: {src} · {regen_line(args)}", "",
           "| Файл | Строк | Функций | Назначение |", "|---|---|---|---|"]
    for f in files:
        n = sum(1 for it in f["items"] if it["kind"] != "class")
        out.append(f"| `{esc(f['path'])}` | {f['lines']} | {n} | {esc(cut(file_desc(f, sc), args.max_doc))} |")
    return "\n".join(out) + "\n"


# ───────────────────────────── diff ─────────────────────────────

def _items_index(model: dict) -> dict:
    idx = {}
    for f in model["files"]:
        seen = Counter()
        for it in f["items"]:
            base = (it["kind"], it["name"])
            key = (f["path"],) + base + (seen[base],)
            seen[base] += 1
            idx[key] = it
    return idx


def _fingerprint(it: dict):
    return (it.get("args", ""), it.get("returns", ""), tuple(it.get("decorators", [])),
            tuple((r["method"], r["full"], tuple(r["deps"])) for r in it.get("routes", [])),
            tuple((h["event"], h["filters"]) for h in it.get("handlers", [])),
            tuple(it.get("states", [])) if "states" in it else None)


def _item_line(it: dict) -> str:
    return f"`{display(it, 140)}`"


def diff_models(a: dict, b: dict, name_a: str, name_b: str) -> str:
    fa = {f["path"]: f for f in a["files"]}
    fb = {f["path"]: f for f in b["files"]}
    ia, ib = _items_index(a), _items_index(b)
    added_files = sorted(set(fb) - set(fa))
    removed_files = sorted(set(fa) - set(fb))
    added = [k for k in ib if k not in ia and k[0] in fa]
    removed = [k for k in ia if k not in ib and k[0] in fb]
    changed = [k for k in ib if k in ia and _fingerprint(ia[k]) != _fingerprint(ib[k])]

    def routes(m):
        out = {}
        for f in m["files"]:
            for it in f["items"]:
                for r in it.get("routes", []):
                    out[(r["method"], r["full"])] = (it["name"], f["path"], tuple(r["deps"]))
        return out

    def handlers(m):
        out = {}
        for f in m["files"]:
            for it in f["items"]:
                for h in it.get("handlers", []):
                    out[(f["path"], it["name"], h["event"])] = h["filters"]
        return out

    ra, rb, ha, hb = routes(a), routes(b), handlers(a), handlers(b)
    n_new = sum(len(fb[p]["items"]) for p in added_files)
    n_old = sum(len(fa[p]["items"]) for p in removed_files)
    out = [f"# codemap diff: {name_a} → {name_b}", "",
           f"Что нового в `{name_b}` относительно `{name_a}` (добавлено — есть в {name_b}, нет в {name_a}); номера строк не сравниваются.", "",
           f"Сводка: файлов +{len(added_files)} −{len(removed_files)}; элементов (функции, классы) +{len(added) + n_new} "
           f"−{len(removed) + n_old} ~{len(changed)}; роутов +{len(set(rb) - set(ra))} −{len(set(ra) - set(rb))}; "
           f"хендлеров +{len(set(hb) - set(ha))} −{len(set(ha) - set(hb))}.", ""]
    if set(rb) - set(ra) or set(ra) - set(rb) or any(ra[k] != rb[k] for k in set(ra) & set(rb)):
        out += ["## Роуты", "", "| Изменение | Метод | Путь | Функция | Файл |", "|---|---|---|---|---|"]
        for k in sorted(set(rb) - set(ra)):
            out.append(f"| + | {k[0]} | `{esc(k[1])}` | `{rb[k][0]}` | {rb[k][1]} |")
        for k in sorted(set(ra) - set(rb)):
            out.append(f"| − | {k[0]} | `{esc(k[1])}` | `{ra[k][0]}` | {ra[k][1]} |")
        for k in sorted(k for k in set(ra) & set(rb) if ra[k] != rb[k]):
            out.append(f"| ~ | {k[0]} | `{esc(k[1])}` | `{ra[k][0]}` → `{rb[k][0]}` | {rb[k][1]} |")
        out.append("")
    if set(hb) - set(ha) or set(ha) - set(hb) or any(ha[k] != hb[k] for k in set(ha) & set(hb)):
        out += ["## Хендлеры бота", "", "| Изменение | Событие | Фильтры | Функция | Файл |", "|---|---|---|---|---|"]
        for k in sorted(set(hb) - set(ha)):
            out.append(f"| + | {k[2]} | `{esc(hb[k])}` | `{k[1]}` | {k[0]} |")
        for k in sorted(set(ha) - set(hb)):
            out.append(f"| − | {k[2]} | `{esc(ha[k])}` | `{k[1]}` | {k[0]} |")
        for k in sorted(k for k in set(ha) & set(hb) if ha[k] != hb[k]):
            out.append(f"| ~ | {k[2]} | `{esc(ha[k])}` → `{esc(hb[k])}` | `{k[1]}` | {k[0]} |")
        out.append("")
    if added_files:
        out += ["## Новые файлы", ""]
        for p in added_files:
            its = fb[p]["items"]
            names = ", ".join(f"`{it['name']}`" for it in its[:12]) + (" …" if len(its) > 12 else "")
            out.append(f"- `{p}` — {len(its)} элем.: {names}" if its else f"- `{p}`")
        out.append("")
    if removed_files:
        out += ["## Удалённые файлы", ""]
        out += [f"- `{p}` — {len(fa[p]['items'])} элем." for p in removed_files]
        out.append("")
    per: "OrderedDict[str, list[str]]" = OrderedDict()
    for k in added:
        per.setdefault(k[0], []).append(f"- + {_item_line(ib[k])}")
    for k in removed:
        per.setdefault(k[0], []).append(f"- − {_item_line(ia[k])}")
    for k in changed:
        x, y = ia[k], ib[k]
        why = []
        if (x.get("args"), x.get("returns")) != (y.get("args"), y.get("returns")):
            why.append(f"сигнатура `{cut(x.get('args', ''), 80)}` → `{cut(y.get('args', ''), 80)}`")
        if x.get("decorators") != y.get("decorators"):
            why.append("декораторы " + ", ".join(f"`@{cut(d, 40)}`" for d in y.get("decorators", [])))
        if [r["full"] for r in x.get("routes", [])] != [r["full"] for r in y.get("routes", [])]:
            why.append("роут")
        if [h["filters"] for h in x.get("handlers", [])] != [h["filters"] for h in y.get("handlers", [])]:
            why.append("фильтры хендлера")
        if x.get("states") != y.get("states"):
            why.append("состояния FSM")
        per.setdefault(k[0], []).append(f"- ~ `{y['name']}`: " + ("; ".join(why) or "изменён"))
    if per:
        out += ["## Изменения в общих файлах", ""]
        for p in sorted(per):
            out += [f"### `{p}`", ""] + per[p] + [""]
    return "\n".join(out).rstrip("\n") + "\n"


def diff_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="codemap.py diff", description="Сравнить две карты (.json): добавлено/удалено/изменено")
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--out")
    ns = ap.parse_args(argv)
    a = json.loads(Path(ns.a).read_text(encoding="utf-8"))
    b = json.loads(Path(ns.b).read_text(encoding="utf-8"))
    md = diff_models(a, b, a.get("name") or Path(ns.a).stem, b.get("name") or Path(ns.b).stem)
    if ns.out:
        Path(ns.out).write_text(md, encoding="utf-8", newline="\n")
        print(f"codemap diff: записан {ns.out}", file=sys.stderr)
    else:
        sys.stdout.write(md)
    return 0


# ───────────────────────────── main ─────────────────────────────

def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "diff":
        return diff_main(argv[1:])
    ap = argparse.ArgumentParser(prog="codemap.py", description="Карта кода: файлы → классы, функции, роуты, хендлеры (AST + regex). "
                                 "Сравнение двух карт: codemap.py diff a.json b.json")
    ap.add_argument("roots", nargs="+", help="папки проекта (можно несколько: Python и GAS)")
    ap.add_argument("--version", action="version", version="codemap " + VERSION)
    ap.add_argument("--lang", default="py,gs,js,html", help="языки: py,gs,js,html (по умолчанию все)")
    ap.add_argument("--include", action="append", default=[], help="только пути/маски (можно несколько)")
    ap.add_argument("--exclude", action="append", default=[], help="пропустить пути/папки/маски (можно несколько)")
    ap.add_argument("--depth", type=int, default=0, help="глубина папок: 1 — только файлы корня (0 — без ограничения)")
    ap.add_argument("--full-doc", action="store_true", help="описание функции — весь докстринг, а не первый абзац")
    ap.add_argument("--nested", action="store_true", help="включать и вложенные функции (по умолчанию — только верхний уровень, методы классов и вложенные хендлеры/роуты)")
    ap.add_argument("--title", help="заголовок H1")
    ap.add_argument("--about", help="одна фраза «о чём» под заголовком")
    ap.add_argument("--files-desc", help="sidecar с описаниями файлов (по умолчанию <root>/codemap.files.md)")
    ap.add_argument("--require-desc", action="store_true", help="ошибка, если у файла нет описания в sidecar")
    ap.add_argument("--regen", help="команда регенерации для шапки (по умолчанию — собрана из аргументов)")
    ap.add_argument("--out", help="файл Markdown (по умолчанию — stdout)")
    ap.add_argument("--json", help="файл JSON с моделью (для diff)")
    ap.add_argument("--no-md", action="store_true", help="не выводить Markdown (только --json и --overview)")
    ap.add_argument("--overview", help="таблица «файл · строк · функций · назначение» в этот файл")
    ap.add_argument("--overview-title", help="заголовок обзора файлов")
    ap.add_argument("--max-doc", type=int, default=200, help="длина описания функции в таблице (по умолчанию 200)")
    ap.add_argument("--max-sig", type=int, default=90, help="длина списка аргументов в таблице (по умолчанию 90)")
    args = ap.parse_args(argv)

    global FULL_DOC
    FULL_DOC = args.full_doc
    roots = args.roots
    name = Path(roots[0]).resolve().name
    sc_path = Path(args.files_desc) if args.files_desc else Path(roots[0]) / "codemap.files.md"
    sc = Sidecar(sc_path)

    files = analyze(collect(roots, args), args)
    if sc.files:
        files.sort(key=lambda f: (sc.order(f) is None, sc.order(f) if sc.order(f) is not None else 0))
    if args.require_desc:
        missing = [f["path"] for f in files if not sc.lookup(f)]
        if missing:
            sys.exit("codemap: нет описания в " + str(sc_path) + " для: " + ", ".join(missing))

    if not args.no_md:
        md = render_md(files, args, sc, roots, name)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(md, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(md)
    if args.overview:
        Path(args.overview).parent.mkdir(parents=True, exist_ok=True)
        Path(args.overview).write_text(render_overview(files, args, sc, roots, name), encoding="utf-8", newline="\n")
    if args.json:
        model = {"codemap": SCHEMA, "version": VERSION, "name": name, "roots": [norm_root(r) for r in roots],
                 "generated": datetime.date.today().isoformat(), "files": files}
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(model, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    n_items = sum(len(f["items"]) for f in files)
    n_routes = sum(len(it.get("routes", [])) for f in files for it in f["items"])
    n_hand = sum(len(it.get("handlers", [])) for f in files for it in f["items"])
    print(f"codemap: файлов {len(files)}, элементов {n_items}, роутов {n_routes}, хендлеров {n_hand}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
