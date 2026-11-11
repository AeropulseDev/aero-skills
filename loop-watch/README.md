# loop-watch

Наблюдатель за GitHub, который работает без Claude: новые комментарии в выбранных issue, issue по меткам, новые issue, коммиты в ветке, PR.
Новое пишется в `watch.db` (SQLite), приходит тостом Windows (и, если настроить, в Telegram). Claude только читает непрочитанное, когда его спросят.

## Установка (Windows, один раз)
1. Python 3.9+ (общий, без виртуальной среды — нужна только стандартная библиотека).
2. `config.example.yaml` → `config.yaml`, заполнить `me`, `vip`, `repos`.
3. Токен GitHub: classic с правом `repo` (для чужих репозиториев) или fine-grained «Issues/Contents/Pull requests: Read» для своих.
4. Двойной клик `setup.cmd`: спросит токен (ввод скрыт) и положит его в переменную пользователя `GITHUB_TOKEN`, уберёт токен из `config.yaml`, создаст задачу планировщика `loop-watch` (каждые 15 мин и при входе), покажет пробный тост и сделает первый опрос.

Linux/macOS: `export GITHUB_TOKEN=…` и cron `*/15 * * * * cd /путь/loop-watch && python3 watch.py run` (уведомлений нет — только база и `inbox.md`).

## Команды
`run` · `unread [--all] [N]` · `show <id>` · `ack <id…|all>` · `status` · `backfill ГГГГ-ММ-ДД` · `init` · `notify-test` · `move-token` · `tz`

## Файлы
| Файл | Что | В git |
|---|---|---|
| `watch.py` | логика (стандартная библиотека) | да |
| `setup.cmd`, `setup.ps1` | установка на Windows | да |
| `config.example.yaml` | шаблон настроек | да |
| `SKILL.md` | инструкция для агента | да |
| `config.yaml`, `watch.db`, `inbox*.md`, `watch.log`, `state*.json` | личное | нет |
