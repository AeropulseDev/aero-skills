# Реестр открытых навыков

Версия реестра: 2026-11-11b · публичный, коммитится. Только навыки этого репозитория (aero-skills); командные и закрытые сюда не писать — они в закрытом реестре пользователя.

## loop-watch — наблюдение за GitHub без затрат токенов
Доступ: открытый · Исходник: `aero-skills/loop-watch` · Версия: 2026-11-11b
Что: `watch.py run` из планировщика Windows (задача `loop-watch`, каждые 15 мин, ставит `setup.cmd`) опрашивает GitHub по `config.yaml` — issue по номерам и меткам, новые issue, коммиты, PR; новое — в SQLite `watch.db` с дедупликацией по id, одно уведомление на запуск (тост Windows, по желанию Telegram), важность 🔴/🟡 по упоминаниям, `vip` и ключевым словам. Агент без сети и токена читает `unread`/`show`, отмечает `ack`, чинит по `status`.
Когда: «что нового на гите / в issue», «следи за issue», добавить issue, метку или репозиторий, «loop-watch молчит», дочитать пропущенное (`backfill`).
Облако: loop-watch, наблюдатель, следи, слежение, мониторинг, watch, issue, комментарии, новые issue, метка, label, labels, any, attention!, коммиты, PR, pull request, watch.db, sqlite, inbox.md, config.yaml, watch.py, setup.cmd, планировщик Windows, Task Scheduler, GITHUB_TOKEN, токен, тост, уведомление, Telegram, vip, unread, ack, status, backfill, что нового, active_hours, часовой пояс.

## codemap — карта кода проекта
Доступ: открытый · Исходник: `aero-skills/codemap` · Версия: 2026-10-20
Что: `codemap.py` (только stdlib) строит карту Python / GAS / JS / HTML по AST и regex: файлы → классы, функции, сигнатуры, декораторы; отдельно роуты FastAPI/aiohttp (с префиксом и Depends), хендлеры aiogram 3 с фильтрами, FSM; вывод Markdown по правилам manuals (`<СЕРВИС>_FUNCTION_MAP.md`) и JSON; `codemap.py diff a.json b.json` — что добавлено, удалено, изменено между двумя версиями проекта; описания файлов — sidecar `codemap.files.md` в проекте.
Когда: карта функций/роутов сервиса, регенерация FUNCTION_MAP, «что нового в чужой версии бота/проекта», быстро разобраться в незнакомом коде без чтения файлов целиком.
Облако: codemap, карта кода, карта функций, function map, FUNCTION_MAP, genmap, структура проекта, роуты, эндпоинты, хендлеры, handlers, aiogram, FastAPI, aiohttp, FSM, StatesGroup, Depends, APIRouter, GAS, Apps Script, google.script.run, AST, diff карт, что изменилось, сравнить версии, добавлено удалено изменено, codemap.files.md, sidecar, --json, --overview, --nested, --depth, build_index.

## git-repo-context-economy — экономия контекста при работе с GitHub
Доступ: открытый · Исходник: `aero-skills/git-repo-context-economy` · Версия: 2026-11-05
Что: правила чтения issues/PR/комментариев через gh, git, GitHub API без раздувания контекста: фильтры в запросе, сабагенты для сквозных проходов, батчинг, сводки решений в project docs, учёт того, где выполняется команда (device_bash).
Когда: разбор issues, PR, ревью, комментариев, состояния репозитория, массовые запросы к GitHub API.
Облако: issues, issue, PR, pull request, пулреквест, ревью, review, комментарии, gh, GitHub API, REST, search API, label, milestone, state, since, per_page, пагинация, фильтр, сабагент, subagent, батч, параллельные запросы, decisions log, сводка, project docs, project_write, контекст, токены, экономия, git fetch, device_bash, где выполняется команда.
