---
name: brain-aero
description: "Локальный текстовый индекс Aeropulse: поиск по мануалам, передачам и базе знаний через SQLite FTS5."
---

# brain-aero

Минимальный шаг 2: `brain.py` индексирует источники `manual`, `handoff` и `kb` в производную локальную SQLite-базу с FTS5.

## Команды

```text
python brain.py build [--full] [--verbose] [--config PATH]
python brain.py find QUERY [--kind KIND] [--since YYYY-MM-DD] [-n N] [--config PATH]
python brain.py stats [--config PATH]
```

Пути источников и базы в конфигурации JSON разрешаются относительно каталога конфигурации. `brain.db`, пользовательский конфиг и `eval.txt` локальны и исключены из git. Исходные тексты не изменяются; вывод `find` содержит только указатель, диапазон строк и короткий фрагмент.

Шаги `map`, `node`, `brief`, `eval`, `issue` и `todo` будут добавлены позднее.
