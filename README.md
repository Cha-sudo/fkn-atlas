# ФКН Атлас

Локальная образовательная библиотека с проверяемым происхождением материала. Чистый перезапуск 16 ноября 2026 года: код и схема созданы заново, старые данные не импортированы.

Первая законченная итерация: от страницы Wiki через внешний источник и исходные байты до постраничного текста, проверенных смысловых фрагментов, проверки кандидата и учебной траектории. Содержательные решения принимает Codex/рецензент по первоисточнику; программа хранит и проверяет основания. Автоматического «понимания всех скачанных файлов» здесь нет.

Восполнение ищется прежде всего среди исторических версий того же курса. Материалы другого курса/программы могут служить заменой только при прямом указании в источнике целевого курса. Иначе это дополнительное чтение, которое не закрывает пробел. В пилоте перенос ПМИ → КНАД отклонён; отдельное полезное сопоставление двух версий ПМИ сохранено.

## Запуск на Windows

Нужен Python 3.11 или 3.12 и Git. В PowerShell:

```powershell
git clone https://github.com/Cha-sudo/fkn-atlas.git
Set-Location fkn-atlas
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test,visual]"
$env:ATLAS_HOME = Join-Path $env:LOCALAPPDATA 'FKNAtlas'
.venv/Scripts/python.exe -X utf8 -m atlas init
.venv/Scripts/python.exe -m pytest -q
```

Активация окружения и изменение ExecutionPolicy не нужны. Если команда `python` открывает Microsoft Store, установите Python либо используйте полный путь к Python из текущего runtime Codex. См. [официальную документацию venv](https://docs.python.org/3/library/venv.html).

```powershell
.venv/Scripts/python.exe -X utf8 -m atlas seed 'https://fruitdet.ru/wiki/Wiki_ФКН' 'https://fruitdet.ru/wiki/Wiki_ФКН/Архив'
.venv/Scripts/python.exe -X utf8 -m atlas run --allow fruitdet.ru --budget 2
.venv/Scripts/python.exe -X utf8 -m atlas links --contains 'Линейная алгебра' --limit 10
# Используйте target_id из результата links:
.venv/Scripts/python.exe -X utf8 -m atlas follow RESOURCE_ID
.venv/Scripts/python.exe -X utf8 -m atlas run --allow fruitdet.ru --budget 1
.venv/Scripts/python.exe -X utf8 -m atlas report
.venv/Scripts/python.exe -X utf8 -m atlas verify
```

`run` обрабатывает только разрешённые домены и ограниченное число уже поставленных задач. Самостоятельного бесконтрольного перехода по всем внешним ссылкам нет. `follow` требует обнаруженное вхождение; `seed` предназначен для явно заданного источника. Повтор: `seed --refresh URL`. Сетевые ошибки сохраняются; повтор с backoff не удаляет успешные снимки.

## Материалы и исследования

```powershell
.venv/Scripts/python.exe -X utf8 -m atlas import 'D:/Library/lecture.pdf' --source-url 'https://example.org/lecture.pdf'
.venv/Scripts/python.exe -X utf8 -m atlas search 'Гаусса'
.venv/Scripts/python.exe -X utf8 -m atlas candidates 'ступенчатая форма свободные переменные' --limit 5
.venv/Scripts/python.exe -X utf8 -m atlas render SNAPSHOT_ID 7 'D:/Library/page-7.png'
.venv/Scripts/python.exe -X utf8 -m atlas plan 'D:/Library/plan.json'
.venv/Scripts/python.exe -X utf8 -m atlas annotate 'D:/Library/fragment.json'
.venv/Scripts/python.exe -X utf8 -m atlas review 'D:/Library/decision.json'
.venv/Scripts/python.exe -X utf8 -m atlas trajectory 'D:/Library/route.json'
.venv/Scripts/python.exe -X utf8 -m atlas report-html (Join-Path $env:ATLAS_HOME 'REPORT.html')
```

Команды с ID и путями выше — шаблоны, не готовые идентификаторы вашей базы. Реальные результаты первой итерации: [отчёт](docs/RESEARCH-20261116.md). Форматы решений и возможности обработчиков: [архитектура](docs/ARCHITECTURE.md), [продолжение без истории чата](docs/HANDOFF.md).

`candidates` — лексический TF-IDF с символьными 4-граммами по содержимому. Это поиск кандидатов, не смысловая модель и не рейтинг качества. По умолчанию он исключает страницы каталога MediaWiki; `--include-plans` добавляет их. Математические формулы сверяйте на рендере. Уверенность, ясность, полнота, корректность и образовательные функции хранятся отдельно, без общей оценки качества.

HTML, PDF, текст, Jupyter и VTT/SRT читаются без исполнения кода. ZIP и Office пока только инвентаризируются. Видео без транскрипта остаётся непрочитанным. Яндекс Диск и YouTube имеют публичные маршруты метаданных; это не загрузка/анализ всех файлов или просмотр видео. Google Drive/GitLab пока классифицируются и сохраняются как ссылки; полноценный обход этих сервисов ещё не реализован.

Большие материалы, исходная SQLite, извлечённые тексты, подписанные URL и исследовательские фрагменты остаются в `ATLAS_HOME`, вне Git. Локальный HTML-отчёт также приватный. В публичной ветке — только код, собственные тестовые примеры, документация и агрегаты.

## Проверки и границы

20 локальных тестов проверяют историю снимков, повторные ссылки, возобновление и аренды задач, 429/backoff, robots, сетевой периметр, опасные ZIP-пути, точность цитат, независимость кандидатов от решений, границы курса и предпосылки траектории. CI запускает те же тесты на Windows/Ubuntu и Python 3.11/3.12. Онлайн-исследование выполнялось отдельно от тестов.

Это рабочее сквозное ядро, а не завершённая библиотека всех программ. Не реализованы массовая смысловая аннотация, OCR/ASR, анализ доски видео, embedding-поиск, автоматическое доказательство корректности, персональный прогресс и наставник. Локальные модели не интегрируются.

Архив старой истории: [`archive/pre-reboot-20261116`](https://github.com/Cha-sudo/fkn-atlas/tree/archive/pre-reboot-20261116). Основная ветка заменяется только после проверяемой страховки. Настройки публичности не изменяются.
