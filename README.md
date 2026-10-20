# aero-skills

Открытые навыки (skills) для ИИ-агентов Claude — чтобы работа с GitHub через агентов почти не тратила токены: агент не перечитывает то, что не менялось, и просыпается только при новом.

| Навык | Что делает |
|---|---|
| [`loop-watch`](loop-watch/) | Бесплатный индекс изменений: следит за issue (по номерам и меткам), новыми issue, коммитами ветки и PR. `watch.py` — стандартная библиотека Python, без модели; новое пишет выжимками в `inbox.md`. Код выхода `10` — есть новое, агент читает только верх `inbox.md`. Только чтение. |
| [`git-repo-context-economy`](git-repo-context-economy/) | Правила экономичного чтения GitHub (issues, PR, комментарии): фильтры в запросе (`since`, `per_page`, `label:`, `state:`), сквозные проходы через сабагента, батчинг запросов, сводки решений вместо перечитывания. |
| [`skills-index`](skills-index/) | Указатель навыков: «что», «когда» и облако ключевых слов. Агент сверяет задачу с облаками и грузит только подходящие навыки. Свои закрытые навыки добавляйте в личную копию. |

## Установка

```
git clone https://github.com/AeropulseDev/aero-skills
# Claude Code, все проекты:  ~/.claude/skills/   (Windows: %USERPROFILE%\.claude\skills\)
# только проект:             <проект>/.claude/skills/
cp -r aero-skills/skills-index aero-skills/git-repo-context-economy aero-skills/loop-watch ~/.claude/skills/
```

В claude.ai: Settings → Capabilities → Skills → загрузить папку навыка архивом (zip). Навыки подхватываются по `SKILL.md`.

### Настройка `loop-watch`
1. `cp config.example.yaml config.yaml` — заполнить `me`, `timezone` (или `city`), `active_hours`, `repos` (номера issue и/или `labels`).
2. Токен GitHub — в переменную окружения `GITHUB_TOKEN`, не в файл и не в коммит (fine-grained с правами Issues/Contents/Pull requests: Read — для репозиториев своего владельца; для чужих — classic с `repo`).
3. `python watch.py init` (база без «новых»), `python watch.py tz` (проверить часовой пояс).
4. Расписание: Linux/macOS `*/30 * * * * cd /путь/loop-watch && python3 watch.py run`; Windows `schtasks /Create /SC MINUTE /MO 30 /TN loop-watch /TR "python C:\путь\loop-watch\watch.py run"`.

Личные `config.yaml`, `state.json`, `inbox.md` в `.gitignore` — не публикуйте их.

### Правило для агента
> Сначала `skills-index`; по GitHub — `git-repo-context-economy`; «что нового» — только верх `inbox.md`, issue целиком не перечитывать.
