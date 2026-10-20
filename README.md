# aero-skills

Открытые навыки для **любых** ИИ-агентов (Claude, Codex, Cursor, Gemini CLI, Aider, свои на API) — чтобы работа с GitHub через агентов почти не тратила токены: агент не перечитывает то, что не менялось, и просыпается только при новом.

Навык здесь — обычная папка: `SKILL.md` (Markdown: когда применять и что делать) и, где нужно, скрипт. Привязки к конкретному агенту нет.

| Навык | Что делает |
|---|---|
| [`loop-watch`](loop-watch/) | Бесплатный индекс изменений: следит за issue (по номерам и меткам), новыми issue, коммитами ветки и PR. `watch.py` — стандартная библиотека Python, без модели; новое пишет выжимками в `inbox.md`. Код выхода `10` — есть новое, агент читает только верх `inbox.md`. Только чтение. |
| [`git-repo-context-economy`](git-repo-context-economy/) | Правила экономичного чтения GitHub (issues, PR, комментарии): фильтры в запросе (`since`, `per_page`, `label:`, `state:`), сквозные проходы через сабагента, батчинг запросов, сводки решений вместо перечитывания. |
| [`skills-index`](skills-index/) | Указатель навыков: «что», «когда» и облако ключевых слов. Агент сверяет задачу с облаками и грузит только подходящие навыки. Свои закрытые навыки добавляйте в личную копию. |

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
- `loop-watch` — режим с токеном (основной) от агента ничего не требует. Режим без токена (browser) нужен только агенту с браузером, умеющим выполнять JS на странице.

### Настройка `loop-watch`
1. `cp config.example.yaml config.yaml` — заполнить `me`, `timezone` (или `city`), `active_hours`, `repos` (номера issue и/или `labels`).
2. Токен GitHub — в переменную окружения `GITHUB_TOKEN`, не в файл и не в коммит (fine-grained с правами Issues/Contents/Pull requests: Read — для репозиториев своего владельца; для чужих — classic с `repo`).
3. `python watch.py init` (база без «новых»), `python watch.py tz` (проверить часовой пояс).
4. Расписание: Linux/macOS `*/30 * * * * cd /путь/loop-watch && python3 watch.py run`; Windows `schtasks /Create /SC MINUTE /MO 30 /TN loop-watch /TR "python C:\путь\loop-watch\watch.py run"`.

Личные `config.yaml`, `state.json`, `inbox.md` в `.gitignore` — не публикуйте их.

### Правило для агента
> Сначала `skills-index`; по GitHub — `git-repo-context-economy`; «что нового» — только верх `inbox.md`, issue целиком не перечитывать.
