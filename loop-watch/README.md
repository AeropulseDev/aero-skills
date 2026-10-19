# loop-watch

Наблюдатель за GitHub: новые комментарии в выбранных issue, новые issue, а с токеном — ещё коммиты в ветке и PR.
Без изменений — молчит; с изменениями — выжимка в `inbox.md` (+ необязательное уведомление).

## Установка
1. Скопируйте папку себе, `config.example.yaml` → `config.yaml`, заполните `me`, город/часовой пояс, `repos`.
2. Python 3.9+. На Windows для часовых поясов по имени: `pip install tzdata` (иначе берётся системный).
   PyYAML не обязателен.
3. Выберите режим:
   - **с токеном** (без Claude, бесплатно): GitHub → Settings → Developer settings → token
     (classic с правом `repo`; fine-grained «Issues/Contents/Pull requests: Read» подходит только для репозиториев своего владельца). Лучше в переменную окружения
     `GITHUB_TOKEN`, а не в config.yaml. Затем `python watch.py init` и расписание:
     - Windows: `schtasks /Create /SC MINUTE /MO 30 /TN loop-watch /TR "python C:\путь\loop-watch\watch.py run"`
     - Linux/macOS (cron): `*/30 * * * * cd /путь/loop-watch && python3 watch.py run`
   - **без токена** (через Claude со встроенным браузером): войдите в GitHub во встроенном браузере Claude
     и попросите Claude «следи за issue по loop-watch» — он работает по SKILL.md.
4. `python watch.py tz` — проверить часовой пояс и рабочие часы.

## Метки
`labels` в блоке репозитория (только с токеном): не указано — только issue по номерам; `["any"]` — все issue, номерные сначала; `["attention!"]` — ещё и issue с этой меткой. Подробнее — в `SKILL.md` и комментариях `config.example.yaml`.

## Файлы
| Файл | Что | В git |
|---|---|---|
| `watch.py` | логика (стандартная библиотека) | да |
| `probe.js` | опрос через браузер (browser-режим) | да |
| `config.example.yaml` | шаблон настроек | да |
| `SKILL.md` | инструкция для агента | да |
| `config.yaml`, `state.json`, `inbox.md`, `probe.json` | личное | нет |
