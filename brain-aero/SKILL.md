---
name: brain-aero
description: "Локальный текстовый индекс Aeropulse: поиск по мануалам, передачам, базе знаний, issue и задачам через SQLite FTS5."
---

# brain-aero

Версия навыка: 2026-11-19 · Исходник: `aero-skills/brain-aero/SKILL.md`

Шаги 2–4: `brain.py` индексирует `manual`, `handoff`, `kb`, карту через `pm.py`, `wf`, события `issue` из локальной базы loop-watch и `todo` из локального UTF-8 списка задач. База brain — производная локальная SQLite с FTS5; хранит ссылки и показывает карточку `node`.

## Команды

```text
python brain.py build [--full] [--verbose] [--config PATH]
python brain.py find QUERY [--kind KIND] [--since YYYY-MM-DD] [-n N] [--config PATH]
python brain.py brief QUERY [-n N] [--config PATH]
python brain.py eval [PATH] [-n N] [--config PATH]
python brain.py stats [--config PATH]
python brain.py check [--config PATH]
python brain.py node ASSET_ID [--config PATH]
```

Пути источников и базы в конфигурации JSON разрешаются относительно каталога конфигурации. `issue` задаётся путём к базе loop-watch или каталогу с файлом `watch.db`; источник открывается только для чтения. `todo` задаётся путём к UTF-8 файлу с чек-листом; индексируется одна строка на задачу. Например, локально добавьте `{"kind":"todo","root":"PATH/TO/TODO.md"}` в `sources` и замените путь на свой. `eval.txt` содержит строки `K|source:location|query` или `P|source:location|query`; для `manual` и `handoff` поддерживается уточнение раздела через `#часть заголовка`. Строки-комментарии `# baseline|kind|method|K_hits/total|P_hits/total` задают результаты baseline отдельно по методу, например `INDEX.md` и «полный текст».

`brain.db`, пользовательский конфиг и `eval.txt` локальны и исключены из git. Исходные тексты не изменяются; вывод `find` и `brief` содержит указатель, диапазон строки или записи и короткий фрагмент. `brief` собирает свежие issue и передачи по теме, а также только открытые задачи. `--since` исключает записи без даты. Если настроенный источник временно недоступен или SQLite issue-база не читается, прежние индексированные фрагменты сохраняются; чтобы удалить источник из индекса, удалите его запись из конфигурации.

`map` и `wf` используют данные инфоцентра; `node` вызывает `pm.py q node` и показывает связанные индексированные фрагменты. Следующие этапы плана — улучшение `build_index.py`, правила `kb/rules.txt`, полное оформление навыка и при необходимости повторный замер для смыслового поиска.
