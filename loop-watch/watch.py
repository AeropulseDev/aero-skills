#!/usr/bin/env python3
"""loop-watch — дешёвый наблюдатель за GitHub-issue (новые комментарии и новые issue).

Режимы (выбирается автоматически):
  token   — есть github_token в config.yaml или переменная GITHUB_TOKEN: всё делает этот скрипт
            через GitHub REST API (один запрос на репозиторий + комментарии только изменившихся issue).
            Работает без Claude: из планировщика Windows / cron.
  browser — токена нет: агент (Claude со встроенным браузером) выполняет probe.js на github.com,
            сохраняет результат в probe.json и вызывает `watch.py diff`.

Команды:
  python watch.py run          token-режим: проверить, дописать inbox.md; код выхода 0 — нового нет, 10 — есть
  python watch.py cfg-js       browser-режим: напечатать строку `const CFG = …;` для probe.js
  python watch.py diff         browser-режим: сравнить probe.json со state.json, напечатать события
  python watch.py init         записать текущее состояние как базу, ничего не сообщая
  python watch.py tz           показать определённый часовой пояс и «тихие часы»
Только стандартная библиотека (PyYAML — если установлен). Токен никуда не печатается.
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

D = pathlib.Path(__file__).resolve().parent
CFG_PATH = D / "config.yaml"
STATE = D / "state.json"
INBOX = D / "inbox.md"
PROBE = D / "probe.json"

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
                    # элемент-словарь: первая пара в той же строке
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
        r.setdefault("issues", [])
        r.setdefault("report_new_issues", True)
        lab = r.get("labels") or []
        if isinstance(lab, str):
            lab = [lab]
        r["labels"] = [str(x).strip().lower() for x in lab if str(x).strip()]
    return cfg


# ── часовой пояс: config.timezone → config.city → система ───────────────────────────────────
CITY_TZ = {
    "yekaterinburg": "Asia/Yekaterinburg", "ekaterinburg": "Asia/Yekaterinburg", "perm": "Asia/Yekaterinburg",
    "chelyabinsk": "Asia/Yekaterinburg", "tyumen": "Asia/Yekaterinburg", "ufa": "Asia/Yekaterinburg",
    "moscow": "Europe/Moscow", "saint petersburg": "Europe/Moscow", "st petersburg": "Europe/Moscow",
    "kazan": "Europe/Moscow", "sochi": "Europe/Moscow", "nizhny novgorod": "Europe/Moscow",
    "samara": "Europe/Samara", "kaliningrad": "Europe/Kaliningrad", "omsk": "Asia/Omsk",
    "novosibirsk": "Asia/Novosibirsk", "krasnoyarsk": "Asia/Krasnoyarsk", "irkutsk": "Asia/Irkutsk",
    "yakutsk": "Asia/Yakutsk", "vladivostok": "Asia/Vladivostok", "khabarovsk": "Asia/Vladivostok",
    "almaty": "Asia/Almaty", "astana": "Asia/Almaty", "tashkent": "Asia/Tashkent", "bishkek": "Asia/Bishkek",
    "minsk": "Europe/Minsk", "tbilisi": "Asia/Tbilisi", "yerevan": "Asia/Yerevan", "baku": "Asia/Baku",
    "istanbul": "Europe/Istanbul", "dubai": "Asia/Dubai", "belgrade": "Europe/Belgrade",
    "sofia": "Europe/Sofia", "berlin": "Europe/Berlin", "london": "Europe/London", "amsterdam": "Europe/Amsterdam",
}


def tzinfo(cfg):
    name = cfg.get("timezone")
    if not name and cfg.get("city"):
        name = CITY_TZ.get(str(cfg["city"]).strip().lower().replace("-", " "))
        if not name:
            print(f"[loop-watch] город «{cfg['city']}» не знаю — укажите timezone (IANA, напр. Asia/Yekaterinburg)",
                  file=sys.stderr)
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), name
        except Exception:
            print(f"[loop-watch] нет базы часовых поясов для {name} (Windows: pip install tzdata) — беру системный",
                  file=sys.stderr)
    local = dt.datetime.now().astimezone().tzinfo
    return local, f"system {dt.datetime.now().astimezone().strftime('%z')}"


def now(cfg):
    return dt.datetime.now(tzinfo(cfg)[0])


def quiet(cfg):
    """True, если сейчас вне рабочих часов (active_hours: [8, 23])."""
    h = cfg.get("active_hours")
    if not h:
        return False
    hr = now(cfg).hour
    return not (int(h[0]) <= hr < int(h[1]))


# ── состояние и inbox ────────────────────────────────────────────────────────────────────────
def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def inbox_add(blocks):
    head = "# Входящие loop-watch (новые сверху)\n\n"
    old = INBOX.read_text(encoding="utf-8") if INBOX.exists() else head
    body = old[len(head):] if old.startswith(head) else old
    INBOX.write_text(head + "".join(blocks) + body, encoding="utf-8")


def compare(cfg, items, state, init=False):
    """items: [{repo, n, t, u, c}] → события; state обновляется на месте."""
    watched = {(r["repo"], int(n)) for r in cfg["repos"] for n in r["issues"]}
    events, stamp = [], dt.datetime.now().isoformat(timespec="seconds")
    for it in items:
        if "error" in it:
            events.append({"kind": "ERROR", "repo": it["repo"], "msg": str(it["error"])})
            continue
        key = f"{it['repo']}#{it['n']}"
        grp = it.get("grp", 0 if (it["repo"], it["n"]) in watched else 2)   # 0 номер, 1 метка, 2 новый
        old = state.get(key)
        if old is None:
            state[key] = {"u": it["u"], "c": it.get("c") or 0, "t": it["t"], "seen": stamp}
            if not init and grp >= 1 and it.get("new", True):
                events.append({"kind": "NEW_ISSUE", "key": key, **it, "grp": grp})
            continue
        if old["u"] != it["u"] or old["c"] != (it.get("c") or 0):
            if not init and grp <= 1:
                events.append({"kind": "CHANGED", "key": key, "old_c": old["c"], **it, "grp": grp})
            old.update(u=it["u"], c=it.get("c") or 0, t=it["t"], seen=stamp)
    return events


# ── token-режим: GitHub REST API ──────────────────────────────────────────────────────────────
def token(cfg):
    return os.environ.get("GITHUB_TOKEN") or cfg.get("github_token") or None


def gh(path, tok):
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "loop-watch"})
    for attempt in range(3):  # сеть иногда рвётся — до 3 попыток
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            import time
            time.sleep(3)


def mask(text):
    # ИНН ИП (12 цифр) и ФИО в форме «Фамилия Имя Отчество» — маскируем в выжимке
    text = re.sub(r"\b\d{12}\b", "<ИНН>", text)
    return re.sub(r"\b([А-ЯЁ])[а-яё]+ ([А-ЯЁ])[а-яё]+ ([А-ЯЁ])[а-яё]+(вич|вна|ич|кызы|оглы)\b", r"\1*** \2. \3.", text)


def short(body, n=600):
    b = re.sub(r"\s+", " ", body or "").strip()
    return mask(b[:n] + ("…" if len(b) > n else ""))


def run_token(cfg, tok, init=False):
    state, items = load_state(), []
    for r in cfg["repos"]:
        try:
            # один запрос: 30 последних обновлённых issue (PR тоже попадают — отсекаем)
            per = 100 if r["labels"] else 30
            data = gh(f"/repos/{r['repo']}/issues?state=all&sort=updated&direction=desc&per_page={per}", tok)
        except Exception as e:  # noqa: BLE001
            items.append({"repo": r["repo"], "error": getattr(e, "code", type(e).__name__)})
            continue
        known = [int(k.split("#")[1]) for k in state if k.startswith(r["repo"] + "#")]
        max_n = max(known + [0])
        fresh = not known  # репозиторий ещё не в базе: всё, что видим, — стартовая база, не «новое»
        want = set(r["labels"])
        for i in data:
            if "pull_request" in i:
                continue
            n = i["number"]
            names = {(l["name"] if isinstance(l, dict) else str(l)).lower() for l in i.get("labels", [])}
            is_new = n > max_n and not fresh
            if n in r["issues"]:
                grp = 0                      # 0 — issue из списка номеров
            elif want and ("any" in want or names & want):
                grp = 1                      # 1 — issue по меткам (any — все)
            elif r["report_new_issues"] and n > max_n:
                grp = 2                      # 2 — просто новый issue
            else:
                continue
            items.append({"repo": r["repo"], "n": n, "t": i["title"][:80], "u": i["updated_at"],
                          "c": i["comments"], "author": i["user"]["login"], "body": i.get("body") or "",
                          "grp": grp, "new": is_new})
    events = compare(cfg, items, state, init=init)
    me = (cfg.get("me") or "").lower()
    blocks, ts = [], now(cfg).strftime("%Y-%m-%d %H:%M")
    for r in cfg["repos"]:
        blocks += watch_commits_prs(r, tok, state, me, ts, init)
    save_state(state)
    if init:
        print(f"[loop-watch] база записана: {len(state)} записей")
        return 0
    cp_blocks, blocks = blocks, []   # коммиты/PR — после issue
    for e in sorted(events, key=lambda x: x.get("grp", 9)):   # сначала номерные issue, затем по меткам, затем новые
        if e["kind"] == "ERROR":
            print(f"ERROR {e['repo']}: {e['msg']}")
            continue
        repo, n = e["repo"], e["n"]
        if e["kind"] == "NEW_ISSUE":
            blocks.append(f"## {ts} · {repo}#{n} · новый issue · {e['author']}\n«{e['t']}»\n\n{short(e['body'])}\n\n")
            continue
        new_cnt = max(0, e["c"] - e["old_c"])
        if new_cnt == 0:
            continue  # правка без новых комментариев
        com = gh(f"/repos/{repo}/issues/{n}/comments?per_page=100", tok)[-new_cnt:]
        others = [c for c in com if c["user"]["login"].lower() != me]
        if not others:
            continue  # писал только я
        for c in others:
            blocks.append(f"## {ts} · {repo}#{n} · {c['user']['login']}\n«{e['t']}» — {c['html_url']}\n\n"
                          f"{short(c['body'])}\n\n")
    blocks += cp_blocks
    if blocks:
        inbox_add(blocks)
        print(f"[loop-watch] новых событий: {len(blocks)} → {INBOX.name}")
        notify(cfg, len(blocks))
        return 10
    return 0


def watch_commits_prs(r, tok, state, me, ts, init):
    """Новые коммиты в ветке (watch_commits: true, branch: main) и новые/обновлённые PR (watch_prs: true)."""
    out, repo = [], r["repo"]
    if r.get("watch_commits"):
        br = r.get("branch") or "main"
        key = f"{repo}@{br}"
        try:
            cs = gh(f"/repos/{repo}/commits?sha={br}&per_page=20", tok)
            last = state.get(key, {}).get("sha")
            new = []
            for c in cs:
                if c["sha"] == last:
                    break
                new.append(c)
            if cs:
                state[key] = {"sha": cs[0]["sha"], "seen": ts}
            if last and new and not init:
                lines = []
                for c in reversed(new):
                    who = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
                    if who.lower() == me:
                        continue
                    msg = c["commit"]["message"].splitlines()[0][:120]
                    lines.append(f"- `{c['sha'][:7]}` {who}: {mask(msg)} — {c['html_url']}")
                if lines:
                    out.append(f"## {ts} · {repo}@{br} · коммиты: {len(lines)}\n" + "\n".join(lines) + "\n\n")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {repo} commits: {getattr(e, 'code', type(e).__name__)}")
    if r.get("watch_prs"):
        try:
            prs = gh(f"/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=15", tok)
            for p in prs:
                key = f"{repo}!{p['number']}"
                old = state.get(key)
                state[key] = {"u": p["updated_at"], "s": p["state"], "m": bool(p.get("merged_at")), "seen": ts}
                if init or p["user"]["login"].lower() == me and old:
                    continue
                if old is None and p["state"] == "open":
                    out.append(f"## {ts} · {repo} PR #{p['number']} · новый · {p['user']['login']}\n"
                               f"«{p['title'][:100]}» — {p['html_url']}\n\n{short(p.get('body') or '', 400)}\n\n")
                elif old and old["u"] != p["updated_at"]:
                    st = "смерджен" if p.get("merged_at") else ("закрыт" if p["state"] == "closed" else "обновлён")
                    out.append(f"## {ts} · {repo} PR #{p['number']} · {st}\n«{p['title'][:100]}» — {p['html_url']}\n\n")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {repo} pulls: {getattr(e, 'code', type(e).__name__)}")
    return out


def notify(cfg, count):
    """Необязательная команда уведомления из config (notify_cmd), {n} — число событий."""
    cmd = cfg.get("notify_cmd")
    if cmd:
        os.system(str(cmd).replace("{n}", str(count)))


# ── browser-режим ────────────────────────────────────────────────────────────────────────────
def cfg_js(cfg):
    state = load_state()
    out = {"me": cfg.get("me"), "repos": []}
    for r in cfg["repos"]:
        known = [int(k.split("#")[1]) for k in state if k.startswith(r["repo"] + "#")]
        out["repos"].append({"repo": r["repo"], "issues": [int(x) for x in r["issues"]],
                             "report_new_issues": bool(r["report_new_issues"]), "max_n": max(known + [0])})
    print("const CFG = " + json.dumps(out, ensure_ascii=False) + ";")


def diff_browser(cfg, init=False):
    items = json.loads(PROBE.read_text(encoding="utf-8"))
    state = load_state()
    events = compare(cfg, items, state, init=init)
    save_state(state)
    for e in events:
        if e["kind"] == "ERROR":
            print(f"ERROR {e['repo']} {e['msg']}")
        elif e["kind"] == "NEW_ISSUE":
            print(f"NEW_ISSUE {e['key']} c={e.get('c')} «{e['t']}»")
        else:
            print(f"CHANGED {e['key']} comments {e['old_c']}->{e.get('c')} «{e['t']}»")


def main():
    cmd = (sys.argv[1:] or ["run"])[0]
    cfg = load_cfg()
    if cmd == "tz":
        z = tzinfo(cfg)
        print(f"часовой пояс: {z[1]}, сейчас {now(cfg):%Y-%m-%d %H:%M}, тихие часы: {'да' if quiet(cfg) else 'нет'}")
        return 0
    if cmd == "cfg-js":
        return cfg_js(cfg) or 0
    if cmd == "diff":
        return diff_browser(cfg) or 0
    tok = token(cfg)
    if cmd == "init":
        if tok:
            return run_token(cfg, tok, init=True)
        return diff_browser(cfg, init=True) or 0
    if cmd == "run":
        if not tok:
            print("[loop-watch] токена нет — browser-режим: запускайте через агента (см. SKILL.md)")
            return 2
        if quiet(cfg):
            return 0
        return run_token(cfg, tok)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
