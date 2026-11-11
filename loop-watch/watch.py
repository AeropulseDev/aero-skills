#!/usr/bin/env python3
"""loop-watch v2 — наблюдатель за GitHub без Claude: опрос из планировщика Windows, база SQLite, тост/Telegram.

Схема: планировщик Windows раз в 15 минут запускает `pythonw watch.py run` (ноль токенов Claude).
Новое (комментарии, новые issue, коммиты, PR) пишется в watch.db с дедупликацией по id GitHub;
уведомление — одно на запуск: тост Windows (клик открывает ссылку) и, если настроен, Telegram.
Claude нужен только когда владелец спрашивает «что нового»: `watch.py unread` читает базу без сети.

Команды:
  run                    опросить GitHub (нужен токен), записать новое, уведомить; код 0 — нового нет, 10 — есть
  unread [--all] [N]     непрочитанное, важное первым (без сети); --all — и прочитанное, N — сколько (по умолч. 30)
  show <id>              полный текст события
  ack <id …|all>         отметить прочитанным
  status                 когда был последний успешный опрос, ошибки, сколько непрочитанного
  backfill <ГГГГ-ММ-ДД>  дочитать комментарии номерных issue начиная с даты (то, что пропустили раньше)
  init                   начать отсчёт с «сейчас» (старое не сообщать)
  notify-test            показать пробное уведомление
  move-token             перенести github_token из config.yaml в переменную пользователя Windows и стереть из файла
  tz                     часовой пояс и рабочие часы
Только стандартная библиотека. Токен никуда не печатается.
"""
import base64
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

D = pathlib.Path(__file__).resolve().parent
CFG_PATH = D / "config.yaml"
DB = D / "watch.db"
INBOX = D / "inbox.md"
LEGACY_STATE = D / "state.json"
LEGACY_INBOX = D / "inbox_until_2026-11-11.md"
LOG = D / "watch.log"
ACKS = D / "acks.txt"   # отметки «прочитано» от агента: в базу пишет только Windows
OVERLAP = dt.timedelta(minutes=10)
IS_WIN = os.name == "nt"
UTC = dt.timezone.utc


# ── мини-YAML (подмножество: словари, списки, скаляры, [a, b]) ──────────────────────────────
def _scalar(v):
    v = v.strip()
    if not v:
        return None
    if v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v.lower() in ("true", "yes", "да"):
        return True
    if v.lower() in ("false", "no", "нет"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _mini_yaml(text):
    lines = []
    for raw in text.splitlines():
        s = re.sub(r"\s+#.*$", "", raw) if not raw.lstrip().startswith("#") else ""
        if s.strip():
            lines.append((len(s) - len(s.lstrip()), s.strip()))

    def block(i, ind):
        if lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
                body = lines[i][1][2:]
                if ":" in body and not body.startswith(("\"", "'")):
                    lines[i] = (ind + 2, body)
                    item, i = block(i, ind + 2)
                    out.append(item)
                else:
                    out.append(_scalar(body))
                    i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == ind:
            k, _, v = lines[i][1].partition(":")
            i += 1
            if v.strip():
                out[k.strip()] = _scalar(v)
            elif i < len(lines) and lines[i][0] > ind:
                out[k.strip()], i = block(i, lines[i][0])
            else:
                out[k.strip()] = None
        return out, i

    return block(0, lines[0][0])[0] if lines else {}


def load_cfg():
    if not CFG_PATH.exists():
        sys.exit(f"нет {CFG_PATH.name}: скопируйте config.example.yaml в config.yaml и заполните")
    text = CFG_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(text) or {}
    except ImportError:
        cfg = _mini_yaml(text)
    cfg.setdefault("repos", [])
    for r in cfg["repos"]:
        r["issues"] = [int(x) for x in (r.get("issues") or [])]
        r.setdefault("report_new_issues", True)
        lab = r.get("labels") or []
        r["labels"] = [str(x).strip().lower() for x in ([lab] if isinstance(lab, str) else lab) if str(x).strip()]
    cfg["me"] = str(cfg.get("me") or "").lower()
    cfg["vip"] = [str(x).lower() for x in (cfg.get("vip") or [])]
    return cfg


# ── часовой пояс и рабочие часы ──────────────────────────────────────────────────────────────
CITY_TZ = {
    "yekaterinburg": "Asia/Yekaterinburg", "ekaterinburg": "Asia/Yekaterinburg", "perm": "Asia/Yekaterinburg",
    "chelyabinsk": "Asia/Yekaterinburg", "tyumen": "Asia/Yekaterinburg", "ufa": "Asia/Yekaterinburg",
    "moscow": "Europe/Moscow", "saint petersburg": "Europe/Moscow", "kazan": "Europe/Moscow",
    "samara": "Europe/Samara", "kaliningrad": "Europe/Kaliningrad", "omsk": "Asia/Omsk",
    "novosibirsk": "Asia/Novosibirsk", "krasnoyarsk": "Asia/Krasnoyarsk", "irkutsk": "Asia/Irkutsk",
    "vladivostok": "Asia/Vladivostok", "almaty": "Asia/Almaty", "tashkent": "Asia/Tashkent",
    "minsk": "Europe/Minsk", "tbilisi": "Asia/Tbilisi", "yerevan": "Asia/Yerevan", "dubai": "Asia/Dubai",
    "istanbul": "Europe/Istanbul", "berlin": "Europe/Berlin", "london": "Europe/London",
}
_FIXED = {"Asia/Yekaterinburg": 5, "Europe/Moscow": 3, "Europe/Samara": 4, "Asia/Omsk": 6,
          "Asia/Novosibirsk": 7, "Asia/Krasnoyarsk": 7, "Asia/Irkutsk": 8, "Asia/Vladivostok": 10}


def tzinfo(cfg):
    name = cfg.get("timezone") or CITY_TZ.get(str(cfg.get("city") or "").strip().lower().replace("-", " "))
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), name
        except Exception:  # Windows без tzdata: для России смещения постоянные
            if name in _FIXED:
                return dt.timezone(dt.timedelta(hours=_FIXED[name])), name
    loc = dt.datetime.now().astimezone()
    return loc.tzinfo, f"system {loc.strftime('%z')}"


def local(cfg, iso):
    try:
        return parse(iso).astimezone(tzinfo(cfg)[0]).strftime("%d.%m %H:%M")
    except Exception:  # noqa: BLE001
        return str(iso or "")[:16]


def quiet(cfg):
    h = cfg.get("active_hours")
    if not h:
        return False
    hr = dt.datetime.now(tzinfo(cfg)[0]).hour
    return not (int(h[0]) <= hr < int(h[1]))


def parse(iso):
    return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))


def utcnow():
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            LOG.write_text(LOG.read_text(encoding="utf-8")[-50_000:], encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


# ── база ─────────────────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, repo TEXT, num INTEGER, title TEXT,
  author TEXT, url TEXT, created TEXT, body TEXT, prio INTEGER DEFAULT 0, grp INTEGER DEFAULT 9,
  found TEXT, notified TEXT, acked TEXT);
CREATE INDEX IF NOT EXISTS ev_unread ON events(acked, prio);
CREATE TABLE IF NOT EXISTS cursor(repo TEXT PRIMARY KEY, since TEXT);
CREATE TABLE IF NOT EXISTS issues(repo TEXT, num INTEGER, title TEXT, labels TEXT, created TEXT,
  PRIMARY KEY(repo, num));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    """Пишет в базу только Windows. Агент (Linux-машина с примонтированной папкой) — только чтение без блокировок."""
    if not IS_WIN:
        if not DB.exists() or DB.stat().st_size == 0:
            sys.exit("базы ещё нет: на Windows запустите setup.cmd")
        for attempt in range(3):
            try:
                con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro&immutable=1", uri=True)
                con.row_factory = sqlite3.Row
                con.execute("SELECT 1 FROM events LIMIT 1")
                return con
            except sqlite3.DatabaseError:
                time.sleep(2)  # Windows как раз пишет — повторить
        sys.exit("база занята или повреждена — повторите через минуту")
    con = sqlite3.connect(str(DB), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(SCHEMA)
    apply_acks(con)
    return con


def pending_acks():
    try:
        return {x.strip() for x in ACKS.read_text(encoding="utf-8").split() if x.strip()}
    except OSError:
        return set()


def apply_acks(con):
    acks = pending_acks()
    if not acks:
        return
    now = iso(utcnow())
    if "all" in acks:
        con.execute("UPDATE events SET acked=?, notified=COALESCE(notified,?) WHERE acked IS NULL", (now, now))
    con.executemany("UPDATE events SET acked=?, notified=COALESCE(notified,?) WHERE id=? AND acked IS NULL",
                    [(now, now, int(a)) for a in acks if a.isdigit()])
    con.commit()
    ACKS.write_text("", encoding="utf-8")


def kv(con, k, v=None):
    if v is None:
        r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None
    con.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))


_legacy = None


def seen_before(url):
    """Ссылка уже была в старом inbox.md — событие считаем прочитанным (переход с v1)."""
    global _legacy
    if _legacy is None:
        _legacy = LEGACY_INBOX.read_text(encoding="utf-8") if LEGACY_INBOX.exists() else ""
    return bool(url) and url in _legacy


KEYWORDS = re.compile(r"решени|срок|срочн|до \d{1,2}\.\d{2}|дедлайн|блокер|не включаем|утвержд|апрув|аппрув|"
                      r"просьба|прошу|нужно ваше|\?\s*$", re.I | re.M)


def priority(cfg, author, body):
    a, b = (author or "").lower(), body or ""
    if cfg["me"] and re.search(rf"@{re.escape(cfg['me'])}\b", b, re.I):
        return 2
    if a in cfg["vip"]:
        return 2
    return 1 if KEYWORDS.search(b) else 0


def add(con, cfg, key, **e):
    now = iso(utcnow())
    acked = now if seen_before(e.get("url")) else None
    cur = con.execute(
        "INSERT OR IGNORE INTO events(key,kind,repo,num,title,author,url,created,body,prio,grp,found,notified,acked)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, e["kind"], e["repo"], e.get("num"), e.get("title"), e.get("author"), e.get("url"), e.get("created"),
         e.get("body") or "", e.get("prio", priority(cfg, e.get("author"), e.get("body"))), e.get("grp", 9),
         now, acked, acked))
    return cur.rowcount if acked is None else 0


# ── GitHub ───────────────────────────────────────────────────────────────────────────────────
def token(cfg):
    t = os.environ.get("GITHUB_TOKEN")
    if not t and IS_WIN:  # планировщик мог запуститься до setx — читаем переменную пользователя из реестра
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                t = winreg.QueryValueEx(k, "GITHUB_TOKEN")[0]
        except OSError:
            t = None
    return t or cfg.get("github_token") or None


def gh(path, tok, params=None):
    url = "https://api.github.com" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "loop-watch"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(3)


def gh_all(path, tok, params, pages=10):
    out = []
    for pg in range(1, pages + 1):
        chunk = gh(path, tok, {**params, "per_page": 100, "page": pg})
        out += chunk
        if len(chunk) < 100:
            break
    return out


def issue_info(con, tok, repo, n):
    r = con.execute("SELECT * FROM issues WHERE repo=? AND num=?", (repo, n)).fetchone()
    if r:
        return r["title"], set(json.loads(r["labels"] or "[]")), r["created"]
    i = gh(f"/repos/{repo}/issues/{n}", tok)
    save_issue(con, repo, i)
    return i["title"], {l["name"].lower() for l in i.get("labels", [])}, i["created_at"]


def save_issue(con, repo, i):
    labels = [l["name"].lower() if isinstance(l, dict) else str(l).lower() for l in i.get("labels", [])]
    con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?,?)",
                (repo, i["number"], i["title"], json.dumps(labels, ensure_ascii=False), i["created_at"]))


def poll_repo(con, cfg, r, tok):
    repo, me = r["repo"], cfg["me"]
    row = con.execute("SELECT since FROM cursor WHERE repo=?", (repo,)).fetchone()
    start = utcnow()
    if not row:
        # переход с v1: отсчёт с последней записи state.json; новый репозиторий — с «сейчас», молча
        since = dt.datetime.fromtimestamp(LEGACY_STATE.stat().st_mtime, UTC) if LEGACY_STATE.exists() else start
        con.execute("INSERT INTO cursor VALUES(?,?)", (repo, iso(since)))
        if since == start:
            return 0
    else:
        since = parse(row["since"])
    q = iso(since - OVERLAP)
    want, n_new = set(r["labels"]), 0

    def relevant(n, labels, created):
        if n in r["issues"]:
            return 0
        if want and ("any" in want or labels & want):
            return 1
        if r["report_new_issues"] and created and parse(created) >= since - OVERLAP:
            return 2
        return None

    for i in gh_all(f"/repos/{repo}/issues", tok, {"state": "all", "since": q, "sort": "updated", "direction": "asc"}):
        save_issue(con, repo, i)
        if "pull_request" in i or parse(i["created_at"]) < since - OVERLAP or i["user"]["login"].lower() == me:
            continue
        labels = {l["name"].lower() for l in i.get("labels", [])}
        g = relevant(i["number"], labels, i["created_at"])
        if g is not None:
            n_new += add(con, cfg, f"i:{repo}#{i['number']}", kind="issue", repo=repo, num=i["number"],
                         title=i["title"], author=i["user"]["login"], url=i["html_url"], created=i["created_at"],
                         body=i.get("body"), grp=g)

    for c in gh_all(f"/repos/{repo}/issues/comments", tok, {"since": q, "sort": "updated", "direction": "asc"}):
        if c["user"]["login"].lower() == me or parse(c["created_at"]) < since - OVERLAP:
            continue  # свои и правки старых комментариев — не новости
        n = int(c["issue_url"].rsplit("/", 1)[1])
        title, labels, created = issue_info(con, tok, repo, n)
        g = relevant(n, labels, created)
        if g is not None:
            n_new += add(con, cfg, f"c:{c['id']}", kind="comment", repo=repo, num=n, title=title,
                         author=c["user"]["login"], url=c["html_url"], created=c["created_at"], body=c["body"], grp=g)

    if r.get("watch_commits"):
        br = r.get("branch") or "main"
        for c in gh_all(f"/repos/{repo}/commits", tok, {"sha": br, "since": q}, pages=3):
            who = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
            if who.lower() == me:
                continue
            n_new += add(con, cfg, f"s:{c['sha']}", kind="commit", repo=repo, title=f"@{br} {c['sha'][:7]}",
                         author=who, url=c["html_url"], created=c["commit"]["committer"]["date"],
                         body=c["commit"]["message"], prio=0, grp=3)

    if r.get("watch_prs"):
        for p in gh(f"/repos/{repo}/pulls", tok, {"state": "all", "sort": "updated", "direction": "desc", "per_page": 30}):
            if parse(p["updated_at"]) < since - OVERLAP:
                break
            for state, when in (("открыт", p["created_at"]), ("смерджен", p.get("merged_at")),
                                ("закрыт", None if p.get("merged_at") else p.get("closed_at"))):
                if not when or parse(when) < since - OVERLAP:
                    continue
                if state == "открыт" and p["user"]["login"].lower() == me:
                    continue
                n_new += add(con, cfg, f"p:{repo}!{p['number']}:{state}", kind="pr", repo=repo, num=p["number"],
                             title=f"PR {state}: {p['title']}", author=p["user"]["login"], url=p["html_url"],
                             created=when, body=p.get("body") if state == "открыт" else "",
                             prio=1 if state == "смерджен" else 0, grp=3)

    con.execute("UPDATE cursor SET since=? WHERE repo=?", (iso(start), repo))
    return n_new


def run(cfg, tok):
    con = db()
    total, errors = 0, []
    for r in cfg["repos"]:
        try:
            total += poll_repo(con, cfg, r, tok)
            con.commit()
        except urllib.error.HTTPError as e:
            con.rollback()
            errors.append(f"{r['repo']}: HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            con.rollback()
            errors.append(f"{r['repo']}: {type(e).__name__} {e}"[:200])
    if errors:
        fails = int(kv(con, "fails") or 0) + 1
        kv(con, "fails", fails)
        kv(con, "last_error", f"{iso(utcnow())} " + "; ".join(errors))
        log("ERROR " + "; ".join(errors))
        bad_token = any("HTTP 401" in x for x in errors)
        if (bad_token or fails == 4) and not quiet(cfg):  # 401 — сразу; сеть — после часа неудач (4 × 15 мин)
            toast("loop-watch не работает", ["Токен GitHub недействителен — перевыпустите и запустите setup.cmd"
                                             if bad_token else "GitHub не отвечает уже час: " + errors[0]], None)
    else:
        kv(con, "fails", 0)
        kv(con, "last_ok", iso(utcnow()))
        if LEGACY_STATE.exists():  # курсоры перенесены — новые репозитории дальше стартуют молча
            LEGACY_STATE.rename(D / "state.v1.json")
    con.commit()
    sent = 0 if quiet(cfg) else notify_pending(con, cfg)
    render_inbox(con, cfg)
    con.close()
    log(f"run: новых {total}, уведомлено {sent}, ошибок {len(errors)}")
    if total:
        print(f"[loop-watch] новых событий: {total}")
    return 10 if total else 0


# ── вывод: маскировка, уведомления, inbox.md ─────────────────────────────────────────────────
def mask(text):
    text = re.sub(r"\b\d{12}\b", "<ИНН>", text or "")
    return re.sub(r"\b([А-ЯЁ])[а-яё]+ ([А-ЯЁ])[а-яё]+ ([А-ЯЁ])[а-яё]+(вич|вна|ич|кызы|оглы)\b", r"\1*** \2. \3.", text)


def clean(body, n):
    b = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    b = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[картинка]", b)
    b = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", b)
    b = re.sub(r"[#*_`>|]+", " ", b)
    b = re.sub(r"\s+", " ", b).strip()
    return mask(b[:n] + ("…" if len(b) > n else ""))


MARK = {2: "🔴", 1: "🟡", 0: "·"}


def where(e):
    short = e["repo"].split("/")[-1]
    return f"{short}#{e['num']}" if e["num"] else short


def notify_pending(con, cfg):
    rows = con.execute("SELECT * FROM events WHERE notified IS NULL AND acked IS NULL "
                       "ORDER BY prio DESC, grp, created").fetchall()
    if not rows:
        return 0
    imp = sum(1 for e in rows if e["prio"] == 2)
    head = f"GitHub: {len(rows)} нов." + (f", важных {imp}" if imp else "")
    lines = [f"{MARK[e['prio']]} {e['author']} · {where(e)}: {clean(e['body'] or e['title'], 90)}" for e in rows[:3]]
    if len(rows) > 3:
        lines[-1] += f"  (+ещё {len(rows) - 3})"
    toast(head, lines, rows[0]["url"], important=imp > 0)
    telegram(cfg, head, rows)
    con.execute("UPDATE events SET notified=? WHERE id IN (%s)" % ",".join(str(e["id"]) for e in rows),
                (iso(utcnow()),))
    con.commit()
    return len(rows)


def _x(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def toast(title, lines, url, important=False):
    """Тост Windows встроенными средствами (без модулей). Клик — открыть ссылку."""
    if not IS_WIN:
        return False
    url = url or INBOX.as_uri()
    xml = (f'<toast activationType="protocol" launch="{_x(url)}"' + (' scenario="reminder"' if important else "") +
           '><visual><binding template="ToastGeneric">' + f"<text>{_x(title)}</text>" +
           "".join(f"<text>{_x(l)}</text>" for l in lines[:3]) +
           f'</binding></visual><actions><action content="Открыть" activationType="protocol" arguments="{_x(url)}"/>'
           '<action content="Позже" activationType="system" arguments="dismiss"/></actions></toast>')
    b64 = base64.b64encode(xml.encode("utf-8")).decode()
    ps = ("[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
          "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
          f"$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'));"
          "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;$x.LoadXml($s);"
          "$t=New-Object Windows.UI.Notifications.ToastNotification $x;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                       timeout=30, creationflags=0x08000000, capture_output=True)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"toast: {e}")
        return False


def telegram(cfg, head, rows):
    """Необязательно: telegram_chat_id в config.yaml + переменная LOOPWATCH_TG_TOKEN (свой бот от @BotFather)."""
    chat, tok = cfg.get("telegram_chat_id"), os.environ.get("LOOPWATCH_TG_TOKEN")
    if not chat:
        return
    if not tok and IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                tok = winreg.QueryValueEx(k, "LOOPWATCH_TG_TOKEN")[0]
        except OSError:
            tok = None
    if not (chat and tok):
        return
    h = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    parts = [f"<b>{h(head)}</b>"] + [
        f"{MARK[e['prio']]} <b>{h(e['author'])}</b> · <a href=\"{h(e['url'])}\">{h(where(e))}</a>\n"
        f"{h(clean(e['body'] or e['title'], 300))}" for e in rows[:10]]
    data = urllib.parse.urlencode({"chat_id": chat, "text": "\n\n".join(parts)[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        log(f"telegram: {type(e).__name__}")


def render_inbox(con, cfg, keep=300):
    rows = con.execute("SELECT * FROM events ORDER BY created DESC LIMIT ?", (keep,)).fetchall()
    out = ["# Входящие loop-watch (из watch.db, новые сверху; ✅ — прочитано)\n"]
    for e in rows:
        out.append(f"## {'✅ ' if e['acked'] else ''}{MARK[e['prio']]} {local(cfg, e['created'])} · {where(e)} · "
                   f"{e['author']} · [{e['id']}]\n«{(e['title'] or '')[:90]}» — {e['url']}\n\n{clean(e['body'], 600)}\n")
    INBOX.write_text("\n".join(out), encoding="utf-8")


# ── команды для агента и человека ────────────────────────────────────────────────────────────
def cmd_unread(cfg, args):
    con = db()
    limit = next((int(a) for a in args if a.isdigit()), 30)
    acks = pending_acks()
    gone = (lambda e: True) if "all" in acks else (lambda e: str(e["id"]) in acks)
    cond = "" if "--all" in args else "WHERE acked IS NULL"
    rows = [e for e in con.execute(f"SELECT * FROM events {cond} ORDER BY prio DESC, grp, created").fetchall()
            if "--all" in args or not gone(e)]
    left, rows = len(rows), rows[:limit]
    print(f"непрочитано: {left}; последний опрос: {local(cfg, kv(con, 'last_ok'))}")
    for e in rows:
        print(f"[{e['id']}] {MARK[e['prio']]} {local(cfg, e['created'])} {where(e)} {e['kind']} {e['author']} "
              f"{e['url']}\n    {clean(e['body'] or e['title'], 500)}")
    return 0


def cmd_show(args):
    con = db()
    e = con.execute("SELECT * FROM events WHERE id=?", (int(args[0]),)).fetchone()
    if not e:
        print("нет такого id")
        return 1
    print(f"{e['repo']}#{e['num']} «{e['title']}» · {e['author']} · {e['created']}\n{e['url']}\n\n{mask(e['body'])}")
    return 0


def cmd_ack(cfg, args):
    if not IS_WIN:  # агент: отметка в acks.txt, применит ближайший run на Windows
        with ACKS.open("a", encoding="utf-8") as f:
            f.write(" ".join(a for a in args if a.isdigit() or a == "all") + "\n")
        print("отмечено (применится при ближайшем опросе)")
        return 0
    con = db()
    now = iso(utcnow())
    if args == ["all"]:
        n = con.execute("UPDATE events SET acked=?, notified=COALESCE(notified,?) WHERE acked IS NULL", (now, now)).rowcount
    else:
        ids = [int(a) for a in args if a.isdigit()]
        n = con.executemany("UPDATE events SET acked=?, notified=COALESCE(notified,?) WHERE id=?",
                            [(now, now, i) for i in ids]).rowcount
    con.commit()
    render_inbox(con, cfg)
    print(f"отмечено: {n}")
    return 0


def cmd_status(cfg):
    con = db()
    last = kv(con, "last_ok")
    age = (utcnow() - parse(last)).total_seconds() / 60 if last else None
    unread = con.execute("SELECT prio, COUNT(*) c FROM events WHERE acked IS NULL GROUP BY prio").fetchall()
    print(f"последний успешный опрос: {local(cfg, last) if last else 'не было'}"
          + (f" ({age:.0f} мин назад)" if age is not None else ""))
    print(f"ошибок подряд: {kv(con, 'fails') or 0}; последняя ошибка: {kv(con, 'last_error') or '—'}")
    print("непрочитано: " + (", ".join(f"{MARK[r['prio']]} {r['c']}" for r in unread) or "0"))
    print("курсоры: " + "; ".join(f"{r['repo']} с {local(cfg, r['since'])}" for r in con.execute("SELECT * FROM cursor")))
    return 0 if age is not None and age < 45 else 1


def cmd_backfill(cfg, tok, args):
    since = iso(dt.datetime.fromisoformat(args[0]).replace(tzinfo=tzinfo(cfg)[0]))
    con, total = db(), 0
    for r in cfg["repos"]:
        for n in r["issues"]:
            title, _, _ = issue_info(con, tok, r["repo"], n)
            for c in gh_all(f"/repos/{r['repo']}/issues/{n}/comments", tok, {"since": since}):
                if c["user"]["login"].lower() == cfg["me"] or c["created_at"] < since:
                    continue
                total += add(con, cfg, f"c:{c['id']}", kind="comment", repo=r["repo"], num=n, title=title,
                             author=c["user"]["login"], url=c["html_url"], created=c["created_at"], body=c["body"], grp=0)
        con.commit()
    render_inbox(con, cfg)
    print(f"дочитано новых (не было в старом inbox): {total}")
    return 10 if total else 0


def cmd_init():
    con = db()
    con.execute("DELETE FROM cursor")
    con.commit()
    if LEGACY_STATE.exists():
        LEGACY_STATE.rename(D / "state.v1.json")
    print("отсчёт начнётся с ближайшего run")
    return 0


def cmd_move_token():
    if not IS_WIN:
        print("только на Windows")
        return 1
    text = CFG_PATH.read_text(encoding="utf-8")
    m = re.search(r"^github_token:\s*(\S+)\s*$", text, re.M)
    if not m:
        print("в config.yaml токена нет — ничего не делаю")
        return 0
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "GITHUB_TOKEN", 0, winreg.REG_SZ, m.group(1).strip("\"'"))
    CFG_PATH.write_text(text[:m.start()] + "# github_token: перенесён в переменную пользователя GITHUB_TOKEN"
                        + text[m.end():], encoding="utf-8")
    print("токен перенесён в переменную GITHUB_TOKEN и стёрт из config.yaml")
    return 0


def migrate():
    """Один раз: старый inbox.md (v1) → inbox_until_2026-11-11.md; ссылки из него считаются прочитанными."""
    if IS_WIN and INBOX.exists() and not LEGACY_INBOX.exists() and not DB.exists():
        INBOX.rename(LEGACY_INBOX)


def main():
    cmd, args = (sys.argv[1:] or ["run"])[0], sys.argv[2:]
    migrate()
    cfg = load_cfg()
    if cmd == "unread":
        return cmd_unread(cfg, args)
    if cmd == "show" and args:
        return cmd_show(args)
    if cmd == "ack" and args:
        return cmd_ack(cfg, args)
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "init":
        return cmd_init()
    if cmd == "move-token":
        return cmd_move_token()
    if cmd == "tz":
        z = tzinfo(cfg)
        print(f"часовой пояс: {z[1]}, сейчас {dt.datetime.now(z[0]):%Y-%m-%d %H:%M}, тихие часы: {'да' if quiet(cfg) else 'нет'}")
        return 0
    if cmd == "notify-test":
        ok = toast("loop-watch: проверка", ["Так будут выглядеть уведомления о новом на GitHub"], None)
        print("тост показан" if ok else "тост не показан (не Windows или ошибка — см. watch.log)")
        return 0
    tok = token(cfg)
    if cmd in ("run", "backfill") and not tok:
        print("[loop-watch] нет токена: запустите setup.cmd (переменная GITHUB_TOKEN)")
        log("нет токена")
        return 2
    if cmd == "run":
        return run(cfg, tok)
    if cmd == "backfill" and args:
        return cmd_backfill(cfg, tok, args)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
