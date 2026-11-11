# Finance Bot

Telegram-бот для учёта расходов. Работает только в личном чате. Суммы — целые положительные рубли до 9223372036854775807, описание — непустой текст до 255 символов.

Python 3.10+; установка и запуск из PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:BOT_TOKEN = 'ваш токен от BotFather'
.\.venv\Scripts\python.exe bot.py
```

BOT_TOKEN обязателен. Значения из `.env` автоматически не загружаются. Токен храните вне исходников. Если токен раньше публиковался, отзовите его у BotFather.

Для подготовленного локального окружения `.venv-audit` можно сохранить `BOT_TOKEN=...` в исключённом из Git файле `.env` и выполнить `powershell -ExecutionPolicy Bypass -File .\start.ps1`. Этот скрипт читает токен без вывода на экран и передаёт его боту через окружение.

По умолчанию SQLite-файл `db.sqlite` расположен в каталоге проекта независимо от текущей рабочей директории. Необязательная переменная `DATABASE_URL` позволяет указать другую БД:

```powershell
$env:DATABASE_URL = 'sqlite+aiosqlite:///C:/absolute/path/to/finance.sqlite'
```

Существующая таблица расходов совместима; схема и старые записи не мигрируются. `/start` сбрасывает текущий диалог. Отчёты за месяц разбиваются на сообщения при превышении лимита Telegram.

Проверки без Telegram и рабочей БД:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты используют временные SQLite-файлы и имитацию сетевых ответов. Файл `reqiurements.txt` сохранён как ссылка на `requirements.txt` для совместимости старых команд установки.
