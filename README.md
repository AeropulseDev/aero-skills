# aero-skills

Открытые навыки для **любых** ИИ-агентов (Claude, Codex, Cursor, Gemini CLI, Aider, свои на API) — чтобы работа с GitHub через агентов почти не тратила токены: агент не перечитывает то, что не менялось, и просыпается только при новом.

Навык здесь — обычная папка: `SKILL.md` (Markdown: когда применять и что делать) и, где нужно, скрипт. Привязки к конкретному агенту нет.

| Навык | Что делает |
|---|---|
| [`loop-watch`](loop-watch/) | Наблюдатель за GitHub без модели: issue (по номерам и меткам), новые issue, коммиты ветки, PR. `watch.py` (стандартная библиотека Python) запускается планировщиком Windows раз в 15 минут, новое пишет в SQLite `watch.db` с дедупликацией по id и шлёт одно уведомление (тост Windows, по желанию Telegram). Агент только читает непрочитанное (`unread`, `show`) и отмечает (`ack`) — без сети и токена. Только чтение GitHub. |
| [`git-repo-context-economy`](git-repo-context-economy/) | Правила экономичного чтения GitHub (issues, PR, комментарии): фильтры в запросе (`since`, `per_page`, `label:`, `state:`), сквозные проходы через сабагента, батчинг запросов, сводки решений вместо перечитывания. |
| [`codemap`](codemap/) | Карта кода проекта: Python, Google Apps Script, JS, HTML — файлы, классы, функции, роуты FastAPI/aiohttp, хендлеры aiogram, FSM; Markdown по правилам мануалов и JSON; `diff` — что изменилось между двумя версиями проекта. Один скрипт `codemap.py`, только стандартная библиотека. |
| [`skills-index`](skills-index/) | Указатель навыков: «что», «когда» и облако ключевых слов. Индекс собирается из двух реестров: открытый `registry.open.md` (в репозитории) и ваш личный `registry.closed.md` (в `.gitignore`). Как завести свои закрытые навыки — в [README](skills-index/README.md). |

## Установка

```
git clone https://github.com/AeropulseDev/aero-skills
```

Подключите так, как принято в вашем агенте:
- **Claude Code / claude.ai** — скопировать нужные папки в `~/.claude/skills/` (Windows: `%USERPROFILE%\.claude\skills\`) или `<проект>/.claude/skills/`; в claude.ai — Settings → Capabilities → Skills, загрузить папку архивом (zip). Подхватываются по `SKILL.md` сами.
- **Любой другой агент** (Codex, Cursor, Gemini CLI, Aider, свой на API) — держать репозиторий рядом с проектом и одной строкой в файле инструкций агента (`AGENTS.md`, rules, системный промпт) написать: «начни с `aero-skills/skills-index/SKILL.md`». Агент читает указатель и сам открывает нужный `SKILL.md`.
- **Без агента** — `loop-watch/watch.py` обычный Python-скрипт, работает по cron/планировщику сам по себе.

Что учесть другим агентам:
- `skills-index` — универсален.
- `git-repo-context-economy` — принципы универсальны; в тексте встречаются инструменты Claude (Agent, `project_write`, `device_bash`), читайте как «сабагент», «постоянное хранилище сводок», «shell на машине пользователя».
- `loop-watch` — работает без агента; агенту достаточно запускать `watch.py unread / show / ack / status` в папке навыка.

### Настройка `loop-watch`
1. `cp config.example.yaml config.yaml` — заполнить `me`, `vip`, `timezone` (или `city`), `active_hours`, `repos` (номера issue и/или `labels`).
2. Windows: двойной клик `setup.cmd` — токен GitHub (скрытый ввод) уходит в переменную пользователя `GITHUB_TOKEN`, создаётся задача планировщика `loop-watch` (каждые 15 мин и при входе), пробный тост и первый опрос. Токен: fine-grained с Issues/Contents/Pull requests: Read — для репозиториев своего владельца; для чужих — classic с `repo`.
3. Linux/macOS: `export GITHUB_TOKEN=…`, cron `*/15 * * * * cd /путь/loop-watch && python3 watch.py run` (без уведомлений — база и `inbox.md`).

Личные `config.yaml`, `watch.db`, `inbox*.md`, `watch.log`, `acks.txt` в `.gitignore` — не публикуйте их.

### Обновление
В каждом `SKILL.md` есть строка `Версия навыка: …`. Агент при каждом использовании сверяет её с исходником (правило в `skills-index`) и подсказывает обновить копию. Копию в `~/.claude/skills` лучше делать ссылкой на клон (`mklink /J` / `ln -s`) — тогда обновление это просто `git pull`.

### Правило для агента
> Сначала `skills-index`; по GitHub — `git-repo-context-economy`; «что нового» — только верх `inbox.md`, issue целиком не перечитывать.
