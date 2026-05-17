# Stars Bot: описание проекта

## Что это

Проект `stars-bot` - асинхронный Telegram-бот на Python для покупки, хранения и вывода Telegram Stars и Telegram Premium. Бот работает через `aiogram 3`, хранит состояние в PostgreSQL через async SQLAlchemy, использует Alembic для миграций, Redis или in-memory очередь для фоновой обработки заказов и интегрируется с CryptoBot, TON и Fragment.

Основные пользовательские сценарии:

- регистрация пользователя через `/start`, выбор языка, реферальная система;
- покупка Stars и Premium для себя или другого пользователя;
- пополнение баланса через CryptoBot или TON;
- вывод Stars/Premium с внутреннего баланса через Fragment;
- создание и активация чеков со Stars или Premium, включая ограничения по получателю, паролю, каналу, Premium-статусу и новым пользователям;
- профиль, история заказов, промокоды, реферальные начисления;
- inline-калькулятор стоимости.

Основные админские сценарии:

- статистика пользователей, заказов, финансов, рефералов, чеков и промокодов;
- управление пользователями, банами, администраторами;
- управление заказами, проблемными заказами, retry/refund/cancel;
- управление Fragment-аккаунтами, приоритетами, сессиями и проверками;
- настройки цен, комиссий, платежных провайдеров и логирования;
- рассылки;
- мониторинг и управление воркерами.

## Архитектура

Ключевой поток покупки/вывода:

1. `src/bot/handlers/*` принимает ввод пользователя и ведет FSM-сценарии.
2. `src/services/*` содержит бизнес-логику: пользователи, заказы, платежи, балансы, настройки, проверки получателей.
3. `src/db/models.py` описывает доменную модель и финансовые сущности.
4. `src/core/queue.py` кладет заказы в очередь: `InMemoryQueue` для разработки и `RedisQueue` для production.
5. `src/workers/supervisor.py` запускает и реконфигурирует воркеры.
6. `src/workers/order_worker.py` обрабатывает очередь заказов, выбирает Fragment-аккаунт, делает retry и обновляет статусы.
7. `src/api_clients/fragment/*` инкапсулирует низкоуровневую работу с Fragment и TON.

Важные гарантии:

- `orders.order_key` используется для идемпотентности заказа;
- статусы заказов меняются через `OrderService`, обычно `pending -> processing -> completed/failed`;
- операции с балансами должны сопровождаться записью в `transactions` и `balance_ledger`;
- для финансовых изменений используются `Decimal`, а не `float`;
- чувствительные данные Fragment-аккаунтов шифруются через `src/core/crypto.py`;
- Redis-очередь ведет `orders:processing` для восстановления после падений.

## Структура

- `run.py` - точка запуска, вызывает `src.bot.main.main()`.
- `src/config.py` - загрузка `.env`, обязательные переменные и Fragment config.
- `src/bot/main.py` - инициализация бота, очереди, supervisor, middleware и router-ов.
- `src/bot/handlers/` - пользовательские и админские обработчики.
- `src/bot/keyboards/` - inline-клавиатуры.
- `src/bot/middlewares/ban_check.py` - проверка банов.
- `src/bot/safe_bot.py` - безопасная обертка над ботом.
- `src/services/` - бизнес-сервисы.
- `src/db/models.py` - SQLAlchemy-модели.
- `src/db/session.py` - async engine и session factory.
- `src/db/migrations/` - Alembic-миграции.
- `src/core/queue.py` - интерфейс очереди, in-memory и Redis реализации.
- `src/core/crypto.py` - шифрование токенов и mnemonic.
- `src/workers/` - фоновые обработчики заказов и supervisor.
- `src/api_clients/fragment/` - Fragment API клиент.
- `src/locales/ru.yml`, `src/locales/en.yml` - локализация.
- `scripts/queue_monitor.py` - мониторинг очереди.
- `tests/` - нагрузочные и интеграционные тесты очередей, Fragment и supervisor.

## Как запускать

Обычный запуск:

```bash
python run.py
```

Зависимости:

```bash
pip install -r requirements.txt
```

Миграции:

```bash
alembic upgrade head
```

Тесты:

```bash
pytest tests/test_fragment_load.py -v -s
python -m tests.test_worker_supervisor
python -m tests.test_worker_supervisor --dry-run --count 3 --username testuser
```

Dry-run режим использует реальные Fragment-аккаунты и реальные API-вызовы, но не должен выполнять реальную оплату TON. Запускать его только осознанно и с корректным `.env`.

## Что нужно сделать перед любыми изменениями

1. Проверить `git status --short`, потому что в рабочем дереве могут быть пользовательские незакоммиченные изменения.
2. Прочитать конкретные handler/service/model, которые затрагивает задача.
3. Если изменение касается БД, проверить текущие модели и миграции, затем добавить Alembic-миграцию.
4. Если изменение касается денег, заказов, чеков или очереди, проверить идемпотентность, повторные клики, retry, отмену и двойное списание.
5. После правок запускать минимально релевантные тесты или хотя бы импорт/статическую проверку измененных модулей.

