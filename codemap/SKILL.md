---
name: codemap
description: Карта кода проекта (Python, Google Apps Script, JS, HTML) — файлы, классы, функции, роуты FastAPI/aiohttp, хендлеры aiogram, FSM; Markdown и JSON; сравнение двух карт. «карта функций», «function map», «что изменилось в коде бота».
---

# codemap — карта кода проекта

Версия навыка: 2026-10-20 · Исходник: `aero-skills/codemap/SKILL.md` (https://github.com/AeropulseDev/aero-skills). Сверка версии — по правилу `skills-index`.

Файлы: `codemap.py` (только стандартная библиотека Python 3.9+), `README.md` (все параметры и формат), `codemap.files.md` — необязательный sidecar с описаниями файлов в самом проекте.
Команды: на Windows — `python codemap.py …`; агент Claude в Linux-оболочке на компьютере — `python3 codemap.py …`.

## Что делает
По AST (Python) и регулярным выражениям (GAS/JS/HTML `<script>`) строит карту: файл → классы, методы, функции (сигнатура, строка, декораторы, первая фраза докстринга). Отдельно находит:
- **HTTP-роуты** — FastAPI (`@router.get|post…`, `APIRouter(prefix=…, dependencies=…)`, `Depends(...)`), aiohttp (`add_get…`, `web.get`);
- **хендлеры бота** — aiogram 3 (`@router.message|callback_query…` с фильтрами, `router.message.register(...)`);
- **FSM** (`StatesGroup` → состояния), **GAS** (`doGet`, `onOpen`…, вызовы `google.script.run`).
Код не импортируется и не исполняется; читает только `.py .gs .js .html` — `.env`, `*.json`, ключи не открываются никогда. Пропускает `.git`, `.venv`, `__pycache__`, `node_modules`, `secrets`, `tmp`, `alembic/versions`, скрытые папки.

## Когда применять
- Нужна карта функций/роутов сервиса для мануалов (`<СЕРВИС>_FUNCTION_MAP.md` — по правилам manuals/AGENTS.md **генерируется скриптом, руками не пишется**).
- Нужно понять, что нового в чужой версии проекта: две карты `--json` → `diff`.
- Перед правкой незнакомого проекта: быстрая карта вместо чтения файлов целиком.

## Команды
```
codemap.py <root> [<root2> …] [--out X.md] [--json X.json] [--no-md] [--overview X.md]
           [--title T] [--about "фраза"] [--regen "команда"] [--files-desc codemap.files.md] [--require-desc]
           [--lang py,gs,js,html] [--include МАСКА] [--exclude МАСКА] [--depth N] [--nested] [--full-doc]
           [--max-doc 200] [--max-sig 90]
codemap.py diff a.json b.json [--out diff.md]
```
- Несколько корней (Python + GAS): `codemap.py orders_bot g_scripts/orders_bot`.
- `--depth 1` — только файлы корня; без него — рекурсивно.
- Описания файлов — `codemap.files.md` в корне проекта (формат в README): заменяют докстринг модуля; разделы «Вступление» и «Прочее» вставляются в карту как есть; `--require-desc` — ошибка, если файл не описан.

## Рабочий цикл
1. Найти корни проекта и, если есть, `codemap.files.md`; команда регенерации записана в шапке существующей карты (`Регенерация: …`) — брать её.
2. Запустить; карту в manuals — в `<СЕРВИС>_FUNCTION_MAP.md` (первая строка `# Заголовок`, затем фраза «о чём», затем «Обновлено · Источник · Регенерация» — это уже делает скрипт).
3. После правки manuals: `python3 build_index.py` в корне manuals, фиксация — по `AGENTS.md` manuals.
4. Сравнение версий: `codemap.py A --json /tmp/a.json`, `codemap.py B --json /tmp/b.json`, `codemap.py diff /tmp/a.json /tmp/b.json` — добавлено / удалено / изменено (сигнатура, декораторы, роуты, фильтры хендлеров); номера строк не сравниваются.
5. Секреты и данные в карту не попадают (только имена и первые фразы докстрингов); если проект хранит ключи в `.py` — это ошибка проекта, но карта их значений не выводит (только имена констант).

## Ограничения
- Роуты и зависимости видны в пределах файла и вызова `include_router`: зависимости, навешанные в другом файле, ищите в строке «Подключает роутеры».
- JS/GAS — регулярные выражения: функции верхнего уровня (в `<script>` — на уровне отступа блока), без классов и методов.
- Динамические роуты (`getattr`, циклы регистрации) не распознаются.
