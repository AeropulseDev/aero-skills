---
name: brain-aero
description: "Локальный текстовый индекс Aeropulse: поиск по мануалам, передачам и базе знаний через SQLite FTS5."
---

# brain-aero

Шаги 2–3: `brain.py` индексирует `manual`, `handoff`, `kb`, карту через `pm.py` и `wf` в производную локальную SQLite-базу с FTS5; хранит ссылки и показывает карточку `node`.

## Команды

```text
python brain.py build [--full] [--verbose] [--config PATH]
python brain.py find QUERY [--kind KIND] [--since YYYY-MM-DD] [-n N] [--config PATH]
python brain.py stats [--config PATH]
python brain.py check [--config PATH]
python brain.py node ASSET_ID [--config PATH]
```

Пути источников и базы в конфигурации JSON разрешаются относительно каталога конфигурации. `brain.db`, пользовательский конфиг и `eval.txt` локальны и исключены из git. Исходные тексты не изменяются; вывод `find` содержит только указатель, диапазон строк и короткий фрагмент.

`map` и `wf` задаются источниками в конфиге; `node` использует `pm.py q node` и связанные ссылки. Шаги `brief`, `eval`, `issue` и `todo` будут добавлены позднее.
