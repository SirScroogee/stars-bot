# Инструкция по деплою Stars Bot

## 1. Что уже проверено

Перед деплоем локально пройдены проверки:

```bash
python -m compileall -q src run.py
python -m pip check
python -m alembic heads
python -m alembic current
python -m pytest tests/test_fragment_load.py -q -k "queue"
python -m tests.test_worker_supervisor --count 2 --delay 0.01
```

Результат:

- зависимости без конфликтов;
- Alembic имеет один head: `19cd1e83e15d`;
- локальная БД на head;
- тесты очереди прошли;
- mock supervisor прошел;
- настройка `news_channel_url` читается из `bot_settings`;
- кнопка новостей появляется только когда `news_channel_url` не пустой;
- стили главного меню сериализуются так:
  - `Звёзды`, `Premium` - `success`;
  - `Пополнить баланс` - `primary`;
  - `Чеки` - без стиля.

## 2. Важное про прокси

На сервере вне РФ прокси должен быть выключен.

В коде это значение должно оставаться таким:

```python
LOCAL_TELEGRAM_PROXY: str | None = None
```

Локально из РФ можно временно запускать с прокси без изменения файла:

```powershell
$code = "import asyncio; import src.bot.main as m; m.LOCAL_TELEGRAM_PROXY='http://127.0.0.1:10808'; asyncio.run(m.main())"
.\.venv\Scripts\python.exe -u -c $code
```

В `.env` переменной `BOT_PROXY` нет намеренно, чтобы прокси случайно не уехал на сервер.

## 3. Что не коммитить

Нельзя коммитить:

- `.env`;
- `.venv/`;
- `logs/`;
- `.pytest_cache/`;
- `.idea/`;
- реальные токены, mnemonic, Fragment cookies/session.

`.env` уже убран из индекса Git и должен оставаться только локальным файлом.

Перед коммитом проверить:

```bash
git status --short
git status --short --ignored
```

В списке коммита не должно быть `.env`.

## 4. Как загрузить проект в Git

1. Посмотреть изменения:

```bash
git status --short
```

2. Добавить нужные файлы:

```bash
git add .gitignore DEPLOY.md .codex .env.example alembic.ini docker-compose.yml requirements.txt run.py scripts src tests
```

Если нужна документация `.claude`, добавить отдельно:

```bash
git add .claude
```

3. Проверить, что `.env` не попал:

```bash
git status --short
```

4. Сделать коммит:

```bash
git commit -m "Prepare bot for deployment"
```

5. Создать удаленный репозиторий на GitHub/GitLab и привязать origin:

```bash
git remote add origin git@github.com:USER/REPO.git
git branch -M main
git push -u origin main
```

Если origin уже есть:

```bash
git remote -v
git push
```

## 5. Как перенести на сервер через Git

На сервере:

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv python3-pip postgresql redis-server
git clone git@github.com:USER/REPO.git stars-bot
cd stars-bot
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Создать `.env` на сервере вручную:

```env
BOT_TOKEN=...
DATABASE_URL=postgresql+asyncpg://stars_user:STRONG_PASSWORD@127.0.0.1:5432/stars_bot
REDIS_URL=redis://127.0.0.1:6379/0
ENCRYPTION_KEY=...
ADMIN_IDS=8157051929,7869155023
DEBUG=false
```

Сгенерировать `ENCRYPTION_KEY`:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 6. Как перенести без Git

Вариант через архив:

1. На локальной машине создать архив без мусора:

```powershell
Compress-Archive -Path .codex,.env.example,.gitignore,DEPLOY.md,alembic.ini,docker-compose.yml,requirements.txt,run.py,scripts,src,tests -DestinationPath stars-bot.zip -Force
```

2. Передать архив на сервер, например через `scp`:

```bash
scp stars-bot.zip user@server:/home/user/
```

3. На сервере:

```bash
unzip stars-bot.zip -d stars-bot
cd stars-bot
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Вариант через `rsync`:

```bash
rsync -av --exclude .git --exclude .env --exclude .venv --exclude logs --exclude .pytest_cache --exclude .idea ./ user@server:/home/user/stars-bot/
```

## 7. Настройка PostgreSQL

Пример:

```bash
sudo -u postgres psql
```

Внутри `psql`:

```sql
create database stars_bot;
create user stars_user with encrypted password 'STRONG_PASSWORD';
grant all privileges on database stars_bot to stars_user;
\q
```

Если PostgreSQL 15+ требует права на schema:

```bash
sudo -u postgres psql -d stars_bot
```

```sql
grant all on schema public to stars_user;
alter schema public owner to stars_user;
\q
```

Проверить миграции:

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

## 8. Redis

Для Ubuntu:

```bash
sudo systemctl enable redis-server
sudo systemctl start redis-server
redis-cli ping
```

Должно вернуть:

```text
PONG
```

## 9. Первый запуск вручную

```bash
cd /home/user/stars-bot
.venv/bin/python run.py
```

Проверить, что в логах есть:

```text
Start polling
Run polling for bot ...
```

Если видишь `TelegramConflictError`, значит где-то запущен второй экземпляр этого же бота. Нужно остановить лишний процесс.

## 10. systemd сервис

Создать файл:

```bash
sudo nano /etc/systemd/system/stars-bot.service
```

Пример:

```ini
[Unit]
Description=Stars Telegram Bot
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
WorkingDirectory=/home/user/stars-bot
EnvironmentFile=/home/user/stars-bot/.env
ExecStart=/home/user/stars-bot/.venv/bin/python run.py
Restart=always
RestartSec=5
User=user

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable stars-bot
sudo systemctl start stars-bot
sudo systemctl status stars-bot
```

Логи:

```bash
journalctl -u stars-bot -f
```

## 11. Настройки после запуска

В админке проверить:

1. `Настройки -> Ссылки`
   - поддержка;
   - новостной канал.

2. `Настройки -> Способы оплаты`
   - CryptoBot token;
   - TON wallet;
   - комиссии.

3. `Fragment`
   - добавить Fragment-аккаунт;
   - проверить статус;
   - пополнить TON, если статус `low_balance`.

4. `Настройки логирования`
   - если появляется `Bad Request: chat not found`, значит лог-чат задан неверно или бот не имеет доступа;
   - либо указать корректный чат/топики, либо временно выключить логирование.

5. Админы:
   - сейчас в локальной БД админы: `8157051929`, `7869155023`;
   - на новой серверной БД их нужно создать через `/start` и выдать права через SQL или админку.

## 12. SQL для выдачи админки

Если пользователь уже запускал бота:

```sql
update users set is_admin = true where id = 8157051929;
update users set is_admin = true where id = 7869155023;
```

Если пользователя еще нет, лучше сначала попросить его нажать `/start`, затем выполнить `update`.

## 13. Финальная проверка после деплоя

```bash
.venv/bin/python -m compileall -q src run.py
.venv/bin/python -m pip check
.venv/bin/python -m alembic current
systemctl status stars-bot
journalctl -u stars-bot -n 100 --no-pager
```

Проверить в Telegram:

- `/start` открывает главное меню;
- кнопка `Новости` видна, если ссылка задана;
- `Звёзды` и `Premium` зеленые, если стиль поддержан текущим Telegram-клиентом;
- `Пополнить баланс` синяя;
- `Чеки` дефолтная;
- админка открывается у нужных пользователей.

