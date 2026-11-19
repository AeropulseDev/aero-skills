#!/usr/bin/env python3
"""Local FTS index with optional FastEmbed vector search (no inference API)."""
import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import math
import re
import sqlite3
import struct
import sys
import time
from pathlib import Path

VALID_KINDS = frozenset(("manual", "handoff", "kb", "map", "wf", "issue", "todo"))
SCHEMA = """CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL, mtime REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,path TEXT NOT NULL,kind TEXT NOT NULL,heading TEXT NOT NULL,line_from INTEGER NOT NULL,line_to INTEGER NOT NULL,date TEXT,text TEXT NOT NULL,stem TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(heading,stem,content='chunks',content_rowid='id',tokenize='unicode61');
CREATE TABLE IF NOT EXISTS links(chunk_id INTEGER NOT NULL,ref_kind TEXT NOT NULL,ref_id TEXT NOT NULL,UNIQUE(chunk_id,ref_kind,ref_id));
CREATE TABLE IF NOT EXISTS embeddings(chunk_id INTEGER NOT NULL,model TEXT NOT NULL,dimension INTEGER NOT NULL,vector BLOB NOT NULL,PRIMARY KEY(chunk_id,model));"""
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Long texts: only the nearest semantic neighbor may compete with lexical hits.
# Map nodes and other short kinds keep the full candidate pool.
DEFAULT_SEMANTIC_POOL = {"manual": 1, "handoff": 1}
EMBEDDER_CACHE = {}
DATE_RE = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")
WORD_RE = re.compile(r"[\w][\w_-]*", re.UNICODE)
ENDINGS = ("иями", "ами", "ого", "ему", "ому", "ими", "ыми", "ов", "ев", "ей", "ам", "ям", "ах", "ях", "ы", "и", "а", "я", "о", "е", "у", "ю")


def normalize_word(w):
    w = w.lower().replace("ё", "е")
    if len(w) >= 5 and not ("_" in w or any(c.isdigit() for c in w) or re.search(r"[a-z]", w)):
        for ending in ENDINGS:
            if w.endswith(ending) and len(w) - len(ending) >= 4:
                return w[:-len(ending)]
    return w


def stem_text(text):
    return " ".join(normalize_word(x) for x in WORD_RE.findall(text))


def read_utf8(path):
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ValueError("UTF-8 decode failed for %s: %s" % (path, exc))


def strict_date(value):
    try:
        dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return value


def date_for(path, text):
    for value in DATE_RE.findall(path.name) + DATE_RE.findall(text[:500]):
        if strict_date(value):
            return value
    return None


def date_values(path, text):
    values = DATE_RE.findall(path.name) + DATE_RE.findall(text[:500])
    return values, [value for value in values if not strict_date(value)]


def markdown_chunks(path, kind):
    text = read_utf8(path)
    lines = text.splitlines()
    headings, fenced = [], False
    for index, line in enumerate(lines):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and not fenced:
            headings.append((index, match.group(2).strip()))
    chunks = []
    if headings:
        for number, (start, heading) in enumerate(headings):
            end = headings[number + 1][0] if number + 1 < len(headings) else len(lines)
            body = "\n".join(lines[start:end]).strip()
            if body:
                chunks.append((heading, start + 1, end, date_for(path, text), body))
    elif text.strip():
        chunks.append((path.stem, 1, len(lines), date_for(path, text), text.strip()))
    return chunks


def kb_chunks(path):
    text = read_utf8(path)
    if path.suffix.lower() in (".md", ".markdown"):
        return markdown_chunks(path, "kb")
    return [(path.stem, index + 1, index + 1, date_for(path, text), line.strip())
            for index, line in enumerate(text.splitlines()) if line.strip()]


def _split_pipe(line):
    return [item.strip() for item in line.split("|")]


def wf_chunks(path):
    """Index workflow metadata and individual records, especially [steps]."""
    text = read_utf8(path)
    lines, section, rows = text.splitlines(), None, {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        match = re.match(r"^\[(\w+)\]$", line)
        if match:
            section = match.group(1)
            continue
        if not line or line.startswith("#"):
            continue
        rows.setdefault(section or "?", []).append((number, line))
    chunks, date = [], date_for(path, text)
    for name, records in rows.items():
        if name == "meta":
            chunks.append((path.stem + " meta", records[0][0], records[-1][0], date,
                           "\n".join(item[1] for item in records)))
            continue
        for line_number, line in records:
            fields = _split_pipe(line)
            record_id = fields[0] if fields else ""
            label = fields[3] if name == "steps" and len(fields) > 3 else (fields[1] if len(fields) > 1 else line[:80])
            chunks.append((f"{path.stem} {name} {record_id} {label}".strip(), line_number, line_number, date, line))
    return chunks


def _pm_for(root):
    scripts = str(Path(root) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import pm
    return pm


def map_snapshot(root):
    root = Path(root)
    raw, mtime = b"", 0
    files = [root / "map.txt"] + sorted((root / "parts").glob("*.txt"))
    for path in files:
        if path.exists():
            raw += path.name.encode() + b"\0" + path.read_bytes()
            mtime = max(mtime, path.stat().st_mtime)
    return raw, mtime


def map_chunks(root):
    """Build map chunks through infocenter.scripts.pm.load(), never a second parser."""
    root = Path(root).resolve()
    pm = _pm_for(root)
    old_root = pm.ROOT
    try:
        pm.ROOT = root
        model = pm.load()
    finally:
        pm.ROOT = old_root
    flows_by_asset = {}
    for flow in model["flows"]:
        for asset_id in (flow["src"], flow["dst"]):
            flows_by_asset.setdefault(asset_id, []).append(pm.fl(flow))
    chunks = []
    for asset_id, asset in model["assets"].items():
        lines = [f"{asset_id} | {asset['type']} | {asset['name']} | {asset['a']}"]
        lines.extend(flows_by_asset.get(asset_id, ()))
        where = asset.get("where", "map.txt:1")
        match = re.match(r"(.+):(\d+)$", where)
        line_number = int(match.group(2)) if match else 1
        chunks.append((f"{asset_id} {asset['name']}", line_number, line_number, None, "\n".join(lines)))
    return chunks


def config_load(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config["_dir"] = path.parent
    for source in config.get("sources", []):
        if source.get("kind") not in VALID_KINDS:
            raise ValueError("unknown source kind: %s" % source.get("kind"))
    embedding_settings(config)
    return config


def source_root(config, source):
    root = Path(source["root"])
    return root if root.is_absolute() else config["_dir"] / root


def embedding_settings(config):
    settings = config.get("embeddings", {})
    if not isinstance(settings, dict):
        raise ValueError("embeddings config must be an object")
    enabled = settings.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("embeddings.enabled must be boolean")
    if not enabled:
        return None
    model = settings.get("model", DEFAULT_EMBEDDING_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("embeddings.model must be a non-empty model name")
    allow_download = settings.get("allow_model_download", False)
    if not isinstance(allow_download, bool):
        raise ValueError("embeddings.allow_model_download must be boolean")
    cache_value = settings.get("cache_dir", ".venv/model-cache")
    if not isinstance(cache_value, str) or not cache_value.strip():
        raise ValueError("embeddings.cache_dir must be a non-empty path")
    cache_dir = Path(cache_value)
    if not cache_dir.is_absolute():
        cache_dir = config["_dir"] / cache_dir
    try:
        threads = int(settings.get("threads", 1))
        semantic_weight = float(settings.get("semantic_weight", 2.0))
        lexical_weight = float(settings.get("lexical_weight", 1.0))
    except (TypeError, ValueError):
        raise ValueError("embedding threads and weights must be numbers")
    if (threads < 1 or not math.isfinite(semantic_weight) or not math.isfinite(lexical_weight)
            or semantic_weight <= 0 or lexical_weight <= 0):
        raise ValueError("embedding threads and weights must be finite and positive")
    return {
        "model": model.strip(), "cache_dir": cache_dir.resolve(), "threads": threads,
        "allow_model_download": allow_download,
        "query_prefix": str(settings.get("query_prefix", "")),
        "document_prefix": str(settings.get("document_prefix", "")),
        "semantic_weight": semantic_weight, "lexical_weight": lexical_weight,
        "semantic_pool": _semantic_pool(settings.get("semantic_pool")),
    }


def _semantic_pool(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("embeddings.semantic_pool must be an object")
    pool = dict(DEFAULT_SEMANTIC_POOL)
    for kind, value in raw.items():
        if kind not in VALID_KINDS:
            raise ValueError("unknown semantic_pool kind: %s" % kind)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("embeddings.semantic_pool values must be positive integers")
        pool[kind] = value
    return pool


def _embedding_key(settings):
    return "%s|q=%s|d=%s" % (settings["model"], settings["query_prefix"], settings["document_prefix"])


def _get_embedder(config):
    settings = embedding_settings(config)
    if settings is None:
        return None, None
    key = (_embedding_key(settings), str(settings["cache_dir"]), settings["threads"], settings["allow_model_download"])
    if key not in EMBEDDER_CACHE:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ValueError("local embeddings need FastEmbed; install brain-aero/requirements-embeddings.txt") from exc
        try:
            EMBEDDER_CACHE[key] = TextEmbedding(
                model_name=settings["model"], cache_dir=str(settings["cache_dir"]),
                threads=settings["threads"], providers=["CPUExecutionProvider"],
                local_files_only=not settings["allow_model_download"],
            )
        except Exception as exc:
            action = "enable embeddings.allow_model_download once to fetch weights" if not settings["allow_model_download"] else "check model name and local cache"
            raise ValueError("cannot load local embedding model %s; %s: %s" % (settings["model"], action, exc)) from exc
    return EMBEDDER_CACHE[key], settings


def _vector(value):
    if hasattr(value, "tolist"):
        value = value.tolist()
    values = tuple(float(x) for x in value)
    if not values or any(not math.isfinite(x) for x in values):
        raise ValueError("embedding vector is empty or non-finite")
    return values


def _pack_vector(values):
    values = _vector(values)
    return len(values), struct.pack("<%sf" % len(values), *values)


def _unpack_vector(dimension, blob):
    if dimension < 1 or len(blob) != dimension * 4:
        raise ValueError("embedding vector has invalid size")
    return struct.unpack("<%sf" % dimension, blob)


def _cosine(left, right):
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    dot = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def source_files(config):
    for source in config.get("sources", []):
        root = source_root(config, source)
        if not root.exists():
            print("warning: source missing: %s" % root, file=sys.stderr)
            continue
        if source["kind"] == "map":
            if (root / "map.txt").exists():
                yield "map", root, root / "map.txt"
            else:
                print("warning: map source missing map.txt: %s" % root, file=sys.stderr)
            continue
        if source["kind"] in ("issue", "todo"):
            if root.is_file():
                yield source["kind"], root.parent, root
                continue
            default_glob = "watch.db" if source["kind"] == "issue" else "**/*"
            pattern = source.get("glob", default_glob)
            found = False
            for path in root.glob(pattern):
                if path.is_file():
                    found = True
                    yield source["kind"], root, path
            if not found:
                print("warning: %s source has no matching files (%s): %s" %
                      (source["kind"], pattern, root), file=sys.stderr)
            continue
        for path in root.glob(source.get("glob", "**/*")):
            if path.is_file():
                yield source["kind"], root, path


def unavailable_sources(config):
    """Roots that are configured but temporarily absent; retain their old chunks."""
    unavailable = []
    for source in config.get("sources", []):
        kind = source["kind"]
        root = source_root(config, source).resolve()
        missing = not root.exists()
        if not missing and kind == "map":
            missing = not (root / "map.txt").is_file()
        if not missing and kind in ("issue", "todo") and root.is_dir():
            pattern = source.get("glob", "watch.db" if kind == "issue" else "**/*")
            missing = not any(path.is_file() for path in root.glob(pattern))
        if missing:
            unavailable.append((kind, root))
    return unavailable


def _under_root(path, root):
    if path == root:
        return True
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _retained_for_unavailable(stored, kind, unavailable):
    stored_kind, _, absolute = stored.partition(":")
    if stored_kind != kind:
        return False
    candidate = Path(absolute).resolve()
    return any(missing_kind == kind and _under_root(candidate, root)
               for missing_kind, root in unavailable)


def _read_issue_rows(path):
    """Read a stable SQLite snapshot; the connection is read-only and query-only."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    for attempt in range(3):
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            return connection.execute(
                "SELECT id,kind,repo,num,title,created,body FROM events "
                "WHERE lower(kind) IN ('issue','comment') ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            reason = str(exc).lower()
            if attempt < 2 and ("locked" in reason or "busy" in reason):
                time.sleep(0.2 * (attempt + 1))
                continue
            raise ValueError("cannot read issue database %s: %s" % (path, exc))
        except sqlite3.DatabaseError as exc:
            raise ValueError("invalid issue database %s: %s" % (path, exc))
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.DatabaseError:
                    pass
                connection.close()


def issue_chunks(path, invalid_dates=None):
    """Read loop-watch events without opening its database for writes."""
    rows = _read_issue_rows(path)
    chunks = []
    for ident, kind, repo, number, title, created, body in rows:
        title = (title or "").strip()
        label = "comment" if str(kind).lower() == "comment" else "issue"
        heading = "%s#%s %s %s" % (repo or "", number or "?", label, title)
        content = "\n".join(part for part in (title, (body or "").strip()) if part)
        date = str(created)[:10] if created and strict_date(str(created)[:10]) else None
        if created and not date and invalid_dates is not None:
            invalid_dates.append(str(created))
        chunks.append((heading.strip(), int(ident), int(ident), date, content or heading))
    return chunks


def issue_snapshot(path):
    """Read and hash parsed rows, including SQLite WAL-visible changes."""
    for _ in range(3):
        chunks = issue_chunks(path)
        # Hash the exact parsed representation rather than the main .db bytes:
        # loop-watch may be using WAL, where updates live in -wal and the main
        # file's bytes/mtime stay unchanged.
        payload = json.dumps(chunks, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        again = issue_chunks(path)
        payload_again = json.dumps(again, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        if payload == payload_again:
            return chunks, hashlib.sha256(payload).hexdigest()
    raise ValueError("issue database changed while it was being indexed: %s" % path)


def todo_chunks(path):
    """Index one UTF-8 checklist record per task, preserving its T-number."""
    text = read_utf8(path)
    chunks = []
    task_re = re.compile(r"^\s*-\s+\[([ xX])\]\s+(T-\d+)\s*[·|–-]?\s*(.*)$")
    for number, raw in enumerate(text.splitlines(), 1):
        match = task_re.match(raw)
        if not match:
            continue
        state, task_id, rest = match.groups()
        status = "x" if state.strip().lower() == "x" else " "
        heading = "%s [%s] %s" % (task_id, status, rest.strip())
        line = raw.strip()
        chunks.append((heading.strip(), number, number, date_for(path, line), line))
    return chunks


def parse_file(kind, path, root=None):
    if kind == "map":
        return map_chunks(root or path.parent)
    if kind == "wf":
        return wf_chunks(path)
    if kind == "issue":
        return issue_chunks(path)
    if kind == "todo":
        return todo_chunks(path)
    return kb_chunks(path) if kind == "kb" else markdown_chunks(path, kind)


def db_path(config):
    path = Path(config.get("database", "brain.db"))
    return path if path.is_absolute() else config["_dir"] / path


def canonical_key(kind, path):
    return "%s:%s" % (kind, Path(path).resolve())


def _refs(kind, path, heading, text, assets):
    refs = []
    if kind == "wf":
        filename = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        refs.append(("wf", filename.rsplit(".", 2)[0]))
    content = heading + "\n" + text
    for asset_id in assets:
        if re.search(r"(?<![\w-])" + re.escape(asset_id) + r"(?![\w-])", content):
            refs.append(("asset", asset_id))
    for pattern, ref_kind in ((r"\bR-\d+\b", "rule"), (r"\bT-\d+\b", "todo"), (r"(?<!\w)#(\d+)\b", "issue")):
        refs.extend((ref_kind, value if ref_kind != "issue" else "#" + value)
                    for value in re.findall(pattern, content, re.I))
    return refs


def _map_assets(config):
    for source in config.get("sources", []):
        if source.get("kind") == "map":
            try:
                root = source_root(config, source).resolve()
                pm = _pm_for(root)
                old_root = pm.ROOT
                try:
                    pm.ROOT = root
                    return set(pm.load()["assets"])
                finally:
                    pm.ROOT = old_root
            except Exception:
                return set()
    return set()


def _populate_embeddings(connection, embedder, settings):
    model = _embedding_key(settings)
    missing = connection.execute(
        "SELECT c.id,c.heading,c.text FROM chunks c LEFT JOIN embeddings e "
        "ON e.chunk_id=c.id AND e.model=? WHERE e.chunk_id IS NULL ORDER BY c.id", (model,)
    ).fetchall()
    embedded = 0
    for offset in range(0, len(missing), 32):
        batch = missing[offset:offset + 32]
        documents = [settings["document_prefix"] + row[1] + "\n" + row[2] for row in batch]
        vectors = list(embedder.embed(documents, batch_size=32))
        if len(vectors) != len(batch):
            raise ValueError("embedding runtime returned a different vector count")
        for row, vector in zip(batch, vectors):
            dimension, blob = _pack_vector(vector)
            connection.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)",
                               (row[0], model, dimension, blob))
            embedded += 1
    return embedded


def build(config, full=False, verbose=False):
    embedder, embed_settings = _get_embedder(config)
    database = db_path(config)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database))
    result = {"indexed": 0, "unchanged": 0, "removed": 0, "errors": 0, "embedded": 0}
    seen = {}
    unavailable = unavailable_sources(config)
    assets = _map_assets(config)
    try:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN")
        for kind, root, path in source_files(config):
            if kind == "map":
                raw, mtime = map_snapshot(root)
                key, parse_root = canonical_key(kind, root), root
                chunks = None
            elif kind == "issue":
                key, parse_root = canonical_key(kind, path), None
                try:
                    chunks, digest = issue_snapshot(path)
                    mtime = path.stat().st_mtime
                except (OSError, ValueError, ImportError) as exc:
                    print("warning: %s" % exc, file=sys.stderr)
                    seen[key] = (kind, None, path.stat().st_mtime if path.exists() else 0)
                    result["errors"] += 1
                    continue
                raw = None
            else:
                raw, mtime = path.read_bytes(), path.stat().st_mtime
                key, parse_root = canonical_key(kind, path), None
                chunks = None
            if kind != "issue":
                digest = hashlib.sha256(raw).hexdigest()
            seen[key] = (kind, digest, mtime)
            old = connection.execute("SELECT sha,mtime FROM files WHERE path=?", (key,)).fetchone()
            if not full and old and old[0] == digest and old[1] == mtime:
                result["unchanged"] += 1
                continue
            try:
                if chunks is None:
                    chunks = parse_file(kind, path, parse_root)
            except (OSError, ValueError, ImportError) as exc:
                print("warning: %s" % exc, file=sys.stderr)
                if kind == "issue":
                    result["errors"] += 1
                    continue
                old_ids = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE path=?", (key,))]
                if old_ids:
                    connection.executemany("DELETE FROM links WHERE chunk_id=?", [(item,) for item in old_ids])
                    connection.executemany("DELETE FROM embeddings WHERE chunk_id=?", [(item,) for item in old_ids])
                connection.execute("DELETE FROM chunks WHERE path=?", (key,))
                connection.execute("DELETE FROM files WHERE path=?", (key,))
                result["errors"] += 1
                continue
            old_ids = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE path=?", (key,))]
            if old_ids:
                connection.executemany("DELETE FROM links WHERE chunk_id=?", [(item,) for item in old_ids])
                connection.executemany("DELETE FROM embeddings WHERE chunk_id=?", [(item,) for item in old_ids])
            connection.execute("DELETE FROM chunks WHERE path=?", (key,))
            connection.execute("DELETE FROM files WHERE path=?", (key,))
            connection.execute("INSERT INTO files VALUES(?,?,?,?)", (key, kind, digest, mtime))
            for heading, line_from, line_to, date, text in chunks:
                cursor = connection.execute(
                    "INSERT INTO chunks(path,kind,heading,line_from,line_to,date,text,stem) VALUES(?,?,?,?,?,?,?,?)",
                    (key, kind, heading, line_from, line_to, date, text, stem_text(heading + " " + text)),
                )
                for ref_kind, ref_id in dict.fromkeys(_refs(kind, key, heading, text, assets)):
                    connection.execute("INSERT OR IGNORE INTO links VALUES(?,?,?)", (cursor.lastrowid, ref_kind, ref_id))
            result["indexed"] += 1
        for path, kind in connection.execute("SELECT path,kind FROM files").fetchall():
            if path not in seen and not _retained_for_unavailable(path, kind, unavailable):
                ids = [row[0] for row in connection.execute("SELECT id FROM chunks WHERE path=?", (path,))]
                if ids:
                    connection.executemany("DELETE FROM links WHERE chunk_id=?", [(item,) for item in ids])
                    connection.executemany("DELETE FROM embeddings WHERE chunk_id=?", [(item,) for item in ids])
                connection.execute("DELETE FROM chunks WHERE path=?", (path,))
                connection.execute("DELETE FROM files WHERE path=?", (path,))
                result["removed"] += 1
        if embedder is not None:
            result["embedded"] = _populate_embeddings(connection, embedder, embed_settings)
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if verbose:
        message = "indexed={indexed} unchanged={unchanged} removed={removed} errors={errors}".format(**result)
        if embedder is not None:
            message += " embedded=%d" % result["embedded"]
        print(message)
    return result


def fts_query(query, synonyms):
    words = [normalize_word(item) for item in WORD_RE.findall(query)]
    expanded = list(words)
    for word in words:
        expanded.extend(normalize_word(item) for item in synonyms.get(word, []))
    return " OR ".join('"%s"' % item.replace('"', '""') for item in dict.fromkeys(expanded) if item) or '""'


def fts_query_all(query, synonyms):
    groups = []
    for raw in WORD_RE.findall(query):
        word = normalize_word(raw)
        if len(word) < 3 and not any(char.isdigit() for char in word):
            continue
        alternatives = [word] + [normalize_word(item) for item in synonyms.get(word, [])]
        tokens = list(dict.fromkeys(item for item in alternatives if item))
        if tokens:
            groups.append("(" + " OR ".join('"%s"' % item.replace('"', '""') for item in tokens) + ")")
    return " AND ".join(groups) if groups else fts_query(query, synonyms)


def _validate_find(kinds, since):
    if kinds:
        unknown = set(kinds) - VALID_KINDS
        if unknown:
            raise ValueError("unknown kind: %s" % ", ".join(sorted(unknown)))
    if since is not None and not strict_date(since):
        raise ValueError("invalid --since date: %s" % since)


def display_path(config, stored):
    if ":" not in stored:
        return stored
    kind, absolute = stored.split(":", 1)
    path = Path(absolute)
    for source in config.get("sources", []):
        if source.get("kind") != kind:
            continue
        root = source_root(config, source).resolve()
        if root.is_file() and path.resolve() == root:
            return "%s:%s" % (kind, path.name)
        try:
            return "%s:%s" % (kind, path.relative_to(root))
        except ValueError:
            pass
    return stored


def _fts_search_rows(connection, config, query, kinds, since, limit, fresh, open_todo, require_all=False):
    sql = ("SELECT c.id,c.kind,c.path,c.line_from,c.line_to,c.heading,c.text,bm25(chunks_fts,3.0,1.0),c.date "
           "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ?")
    synonyms = {normalize_word(k): v for k, v in config.get("synonyms", {}).items()}
    match = fts_query_all(query, synonyms) if require_all else fts_query(query, synonyms)
    args = [match]
    if kinds:
        sql += " AND c.kind IN (%s)" % ",".join("?" * len(kinds))
        args.extend(kinds)
    if since:
        sql += " AND c.date>=?"
        args.append(since)
    if open_todo:
        sql += " AND c.heading LIKE '%[ ]%'"
    sql += " ORDER BY " + ("c.date DESC,8,c.kind,c.path,c.line_from" if fresh else "8,c.kind,c.path,c.line_from")
    sql += " LIMIT ?"
    return [(row[0], row[1:]) for row in connection.execute(sql, args + [limit]).fetchall()]


def search_rows(config, query, kinds=None, since=None, limit=8, fresh=False, open_todo=False):
    _validate_find(kinds, since)
    if limit < 1:
        raise ValueError("result limit must be positive")
    connection = sqlite3.connect(str(db_path(config)))
    try:
        settings = embedding_settings(config)
        fetch_limit = max(200, limit * 20) if settings else limit
        lexical = _fts_search_rows(connection, config, query, kinds, since, fetch_limit, fresh, open_todo)
        if settings is None:
            return [row for _, row in lexical[:limit]]
        # Do not return lexical hits before semantic scoring: otherwise
        # embeddings are bypassed for every query that already has `limit` FTS
        # hits. Lexical and semantic candidates are merged below.
        embedder, settings = _get_embedder(config)
        has_embeddings = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone() is not None
        if not has_embeddings:
            raise ValueError("embedding table is missing; run brain.py build with embeddings enabled")
        model = _embedding_key(settings)
        query_text = settings["query_prefix"] + query
        query_vector = _vector(next(iter(embedder.query_embed(query_text))))
        sql = ("SELECT c.id,c.kind,c.path,c.line_from,c.line_to,c.heading,c.text,c.date,e.dimension,e.vector "
               "FROM embeddings e JOIN chunks c ON c.id=e.chunk_id WHERE e.model=?")
        args = [model]
        if kinds:
            sql += " AND c.kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        if since:
            sql += " AND c.date>=?"
            args.append(since)
        if open_todo:
            sql += " AND c.heading LIKE '%[ ]%'"
        vectors = connection.execute(sql, args).fetchall()
        if not vectors:
            return [row for _, row in lexical[:limit]]
        semantic = []
        for item in vectors:
            row = (item[1], item[2], item[3], item[4], item[5], item[6], 0.0, item[7])
            score = _cosine(query_vector, _unpack_vector(item[8], item[9]))
            semantic.append((item[0], score, row))
        semantic.sort(key=lambda item: (-item[1], item[2][0], item[2][1], item[2][2], item[0]))
        semantic = semantic[:max(200, limit * 20)]
        # Rank stays global so a single-kind search matches the measured gate.
        # The pool drops extra neighbors of long-text kinds before they can outrank a lexical hit.
        pool = settings["semantic_pool"]
        within_kind = {}
        kept_semantic = []
        for rank, (ident, _, row) in enumerate(semantic, 1):
            kind = row[0]
            within_kind[kind] = within_kind.get(kind, 0) + 1
            kind_pool = pool.get(kind)
            if kind_pool is not None and within_kind[kind] > kind_pool:
                continue
            kept_semantic.append((ident, rank, row))
        semantic_rank = {ident: rank for ident, rank, _ in kept_semantic}
        lexical_rank = {ident: rank for rank, (ident, _) in enumerate(lexical, 1)}
        rows_by_id = {ident: row for ident, row in lexical}
        rows_by_id.update({ident: row for ident, _, row in kept_semantic})
        candidates = []
        for ident, row in rows_by_id.items():
            score = 0.0
            if ident in semantic_rank:
                score += settings["semantic_weight"] / (60.0 + semantic_rank[ident])
            if ident in lexical_rank:
                score += settings["lexical_weight"] / (60.0 + lexical_rank[ident])
            candidates.append((ident, row, score))
        candidates.sort(key=lambda item: (item[1][0], item[1][1], item[1][2], item[0]))
        candidates.sort(key=lambda item: item[2], reverse=True)
        if fresh:
            candidates.sort(key=lambda item: item[1][7] or "", reverse=True)
        return [row[:6] + (-score, row[7]) for _, row, score in candidates[:limit]]
    finally:
        connection.close()


def find(config, query, kinds=None, since=None, limit=8):
    rows = search_rows(config, query, kinds, since, limit)
    for kind, path, line_from, line_to, heading, text, _, _ in rows:
        print("%s | %s:%s-%s | %s | %s" % (kind, display_path(config, path), line_from, line_to, heading, " ".join(text.split())[:240]))
    return rows


def _todo_priority(row):
    match = re.search(r"\bP([1-3])\b", row[4] + " " + row[5])
    return int(match.group(1)) if match else 4


def brief(config, query, limit=8):
    """Compact topical digest: recent issue/handoff context plus open TODOs."""
    if limit < 1:
        raise ValueError("result limit must be positive")
    rows = []
    for kind in ("issue", "handoff", "todo"):
        rows.extend(search_rows(config, query, (kind,), limit=1000,
                               fresh=kind in ("issue", "handoff"), open_todo=kind == "todo"))
    candidates = {kind: [row for row in rows if row[0] == kind] for kind in ("issue", "handoff", "todo")}
    for kind in ("issue", "handoff"):
        candidates[kind].sort(key=lambda row: row[6])
        candidates[kind].sort(key=lambda row: row[7] or "", reverse=True)
    candidates["todo"].sort(key=lambda row: (_todo_priority(row), row[6]))
    quota = max(1, (limit + 2) // 3)
    selected = []
    for kind in ("issue", "handoff", "todo"):
        selected.extend(candidates[kind][:quota])
    selected_ids = {(row[0], row[1], row[2]) for row in selected}
    remaining = [row for kind in ("issue", "handoff", "todo") for row in candidates[kind]
                 if (row[0], row[1], row[2]) not in selected_ids]
    remaining.sort(key=lambda row: row[6])
    selected.extend(remaining[:max(0, limit - len(selected))])
    selected = selected[:limit]
    for kind, path, line_from, line_to, heading, text, _, date in selected:
        print("%s | %s | %s:%s-%s | %s | %s" % (
            date or "date?", kind, display_path(config, path), line_from, line_to,
            heading, " ".join(text.split())[:240]))
    return selected


def _matches_expected(config, row, expected):
    source, target = expected.split(":", 1)
    kind, path, _, _, heading, text, _, _ = row
    if source != kind:
        return False
    normalized_path = display_path(config, path).split(":", 1)[-1].replace("\\", "/").lower()
    if kind == "map":
        return heading.split(" ", 1)[0].lower() == target.lower()
    if kind == "issue":
        match = re.fullmatch(r"(.+?)#(\d+)", target)
        return bool(match and heading.split(" ", 1)[0].lower() ==
                    (match.group(1) + "#" + match.group(2)).lower())
    if kind == "todo" and re.fullmatch(r"T-\d+", target, re.I):
        ids = {value.upper() for value in re.findall(r"(?<![\w-])T-\d+(?!\w)", heading + " " + text, re.I)}
        return target.upper() in ids
    path_target, marker, section = target.partition("#") if kind in ("manual", "handoff") else (target, "", "")
    path_target = path_target.replace("\\", "/").lower()
    location_ok = normalized_path.endswith(path_target) or Path(normalized_path).name == Path(path_target).name
    return location_ok and (not marker or section.lower() in heading.lower())


def _eval_ratio(value, path, line_number):
    match = re.fullmatch(r"(\d+)/(\d+)", value)
    if not match:
        raise ValueError("invalid baseline ratio at %s:%d" % (path, line_number))
    hits, count = map(int, match.groups())
    if count < 1 or hits > count:
        raise ValueError("invalid baseline count at %s:%d" % (path, line_number))
    return [hits, count]


def evaluate(config, path, top_n=5):
    """Evaluate K/P rows; optional comment rows store eval_baseline.py results."""
    if top_n < 1:
        raise ValueError("eval top_n must be positive")
    totals, baselines = {}, {}
    for line_number, raw in enumerate(read_utf8(path).splitlines(), 1):
        line = raw.strip()
        if line.startswith("# baseline|"):
            fields = [item.strip() for item in line[2:].split("|")]
            if len(fields) != 5 or fields[1] not in VALID_KINDS or not fields[2]:
                raise ValueError("invalid baseline row at %s:%d" % (path, line_number))
            key = (fields[1], fields[2])
            if key in baselines:
                raise ValueError("duplicate baseline method at %s:%d" % (path, line_number))
            baselines[key] = {"K": _eval_ratio(fields[3], path, line_number),
                              "P": _eval_ratio(fields[4], path, line_number)}
            continue
        if not line or line.startswith("#"):
            continue
        fields = [item.strip() for item in line.split("|", 2)]
        if (len(fields) != 3 or fields[0] not in ("K", "P") or ":" not in fields[1]):
            raise ValueError("invalid eval row at %s:%d" % (path, line_number))
        group, expected, query = fields
        source, target = expected.split(":", 1)
        if source not in VALID_KINDS or not target or not query:
            raise ValueError("invalid eval source, target, or query at %s:%d" % (path, line_number))
        bucket = totals.setdefault(source, {"K": [0, 0], "P": [0, 0]})[group]
        bucket[1] += 1
        # Compare with the source-specific baseline, not against unrelated kinds.
        rows = search_rows(config, query, kinds=[source], limit=top_n)
        bucket[0] += int(any(_matches_expected(config, row, expected) for row in rows))
    print("источник      brain-aero K       brain-aero P       baseline method / K / P")
    for source in ("map", "manual", "handoff", "issue", "todo", "kb", "wf"):
        if source not in totals:
            continue
        def cell(values):
            hits, count = values
            return "%d/%d %d%%" % (hits, count, (100 * hits // count) if count else 0)
        k, p = totals[source]["K"], totals[source]["P"]
        methods = [(method, result) for (kind, method), result in baselines.items() if kind == source]
        if not methods:
            print("%-12s %-18s %-18s —" % (source, cell(k), cell(p)))
        for method, baseline in methods:
            print("%-12s %-18s %-18s %s | %s | %s" %
                  (source, cell(k), cell(p), method, cell(baseline["K"]), cell(baseline["P"])))
    return totals


def node(config, ident):
    connection = sqlite3.connect(str(db_path(config)))
    try:
        rows = connection.execute(
            "SELECT c.kind,c.path,c.line_from,c.line_to,c.heading,c.text FROM links l JOIN chunks c ON c.id=l.chunk_id "
            "WHERE l.ref_kind='asset' AND l.ref_id=? ORDER BY c.kind,c.path,c.line_from", (ident,)
        ).fetchall()
    finally:
        connection.close()
    map_sources = [source for source in config.get("sources", []) if source.get("kind") == "map"]
    if map_sources:
        try:
            root = source_root(config, map_sources[0]).resolve()
            pm = _pm_for(root)
            old_root = pm.ROOT
            try:
                pm.ROOT = root
                model = pm.load()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    pm.query(model, ["node", ident])
                if output.getvalue().strip():
                    print(output.getvalue().rstrip())
            finally:
                pm.ROOT = old_root
        except Exception as exc:
            print("map node unavailable: %s" % exc, file=sys.stderr)
    print("--- связанные фрагменты:")
    for kind, path, line_from, line_to, heading, text in rows:
        print("%s | %s:%s-%s | %s | %s" % (kind, display_path(config, path), line_from, line_to, heading, " ".join(text.split())[:240]))
    return rows


def stats(config):
    database = db_path(config)
    if not database.exists():
        print("files=0 chunks=0 links=0 embeddings=0 models=0")
        return
    connection = sqlite3.connect(str(database))
    try:
        files = connection.execute("SELECT count(*) FROM files").fetchone()[0]
        chunks = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        links = connection.execute("SELECT count(*) FROM links").fetchone()[0]
        has_embeddings = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone() is not None
        embeddings = connection.execute("SELECT count(*) FROM embeddings").fetchone()[0] if has_embeddings else 0
        models = connection.execute("SELECT count(DISTINCT model) FROM embeddings").fetchone()[0] if has_embeddings else 0
    finally:
        connection.close()
    print("files=%d chunks=%d links=%d embeddings=%d models=%d" % (files, chunks, links, embeddings, models))


def check(config):
    unavailable = unavailable_sources(config)
    report = {"duplicates": 0, "invalid_utf8": 0, "invalid_dates": 0, "invalid_issue_db": 0,
              "missing_sources": len(unavailable), "unknown_kinds": 0,
              "integrity": "missing", "fts_integrity": "missing", "orphan_links": 0,
              "embedding_rows": 0, "orphan_embeddings": 0, "invalid_embeddings": 0, "embedding_missing": 0}
    seen = set()
    checked_text = set()
    for source in config.get("sources", []):
        if source.get("kind") not in VALID_KINDS:
            report["unknown_kinds"] += 1
            continue
        for kind, root, path in source_files(dict(config, sources=[source])):
            key = canonical_key(kind, root if kind == "map" else path)
            if key in seen:
                report["duplicates"] += 1
            seen.add(key)
            if key in checked_text:
                continue
            checked_text.add(key)
            if kind == "issue":
                try:
                    invalid = []
                    issue_chunks(path, invalid)
                    report["invalid_dates"] += len(invalid)
                except (OSError, ValueError):
                    report["invalid_issue_db"] += 1
                continue
            try:
                text = read_utf8(path) if kind != "map" else ""
            except (OSError, ValueError):
                report["invalid_utf8"] += 1
                continue
            _, invalid = date_values(path, text)
            report["invalid_dates"] += len(set(invalid))
    database = db_path(config)
    if database.exists():
        connection = sqlite3.connect(str(database))
        try:
            report["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            try:
                connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
                report["fts_integrity"] = "ok"
            except sqlite3.DatabaseError as exc:
                # FTS5's integrity command writes transient state. A database
                # opened read-only (or concurrently locked) cannot run it;
                # report that limitation without turning a healthy DB check
                # into a confusing failure.
                message = str(exc)
                if "readonly" in message.lower() or "locked" in message.lower():
                    report["fts_integrity"] = "not-run: " + message
                else:
                    report["fts_integrity"] = message
            report["orphan_links"] = connection.execute(
                "SELECT count(*) FROM links l LEFT JOIN chunks c ON c.id=l.chunk_id WHERE c.id IS NULL"
            ).fetchone()[0]
            has_embeddings = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'"
            ).fetchone() is not None
            settings = embedding_settings(config)
            if has_embeddings:
                report["embedding_rows"] = connection.execute("SELECT count(*) FROM embeddings").fetchone()[0]
                report["orphan_embeddings"] = connection.execute(
                    "SELECT count(*) FROM embeddings e LEFT JOIN chunks c ON c.id=e.chunk_id WHERE c.id IS NULL"
                ).fetchone()[0]
                report["invalid_embeddings"] = connection.execute(
                    "SELECT count(*) FROM embeddings WHERE dimension<1 OR length(vector)!=dimension*4"
                ).fetchone()[0]
            if settings is not None:
                model = _embedding_key(settings)
                if has_embeddings:
                    report["embedding_missing"] = connection.execute(
                        "SELECT count(*) FROM chunks c LEFT JOIN embeddings e "
                        "ON e.chunk_id=c.id AND e.model=? WHERE e.chunk_id IS NULL", (model,)
                    ).fetchone()[0]
                else:
                    report["embedding_missing"] = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        finally:
            connection.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "stats", "check"):
        command = sub.add_parser(name)
        command.add_argument("--config", default="brain.config.json")
        if name == "build":
            command.add_argument("--full", action="store_true")
            command.add_argument("--verbose", action="store_true")
    command = sub.add_parser("find")
    command.add_argument("query")
    command.add_argument("--config", default="brain.config.json")
    command.add_argument("--kind", action="append")
    command.add_argument("--since")
    command.add_argument("-n", type=int, default=8)
    command = sub.add_parser("brief")
    command.add_argument("query")
    command.add_argument("--config", default="brain.config.json")
    command.add_argument("-n", type=int, default=8)
    command = sub.add_parser("eval")
    command.add_argument("path", nargs="?", default="eval.txt")
    command.add_argument("--config", default="brain.config.json")
    command.add_argument("-n", type=int, default=5)
    command = sub.add_parser("node")
    command.add_argument("id")
    command.add_argument("--config", default="brain.config.json")
    args = parser.parse_args(argv)
    config = config_load(args.config)
    if args.cmd == "build":
        build(config, args.full, args.verbose)
    elif args.cmd == "stats":
        stats(config)
    elif args.cmd == "check":
        print(json.dumps(check(config), ensure_ascii=False, sort_keys=True))
    elif args.cmd == "node":
        node(config, args.id)
    elif args.cmd == "brief":
        brief(config, args.query, args.n)
    elif args.cmd == "eval":
        eval_path = Path(args.path)
        if not eval_path.is_absolute():
            eval_path = config["_dir"] / eval_path
        evaluate(config, eval_path, args.n)
    else:
        find(config, args.query, args.kind, args.since, args.n)


if __name__ == "__main__":
    main()
