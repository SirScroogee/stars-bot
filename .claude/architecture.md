# Architecture — Telegram Stars & Premium Bot

## 1. Общая схема

```
User (Telegram)
    │
    ▼
Bot (aiogram) → Handlers
    │
    ▼
OrderService (создание заказа, idempotent)
    │
    ▼
OrderQueue (in-memory / Redis)
    │
    ▼
OrderWorker
    │
    ▼
PaymentService (бизнес-логика)
    │
    ▼
FragmentClient (низкоуровневый API)
    │
    ▼
Fragment API / TON Blockchain
```

## 2. Структура проекта

```
src/
├── config.py                    # Центральная конфигурация
├── bot/
│   ├── __init__.py
│   ├── main.py                  # Запуск бота
│   ├── handlers/
│   │   ├── start.py
│   │   ├── catalog.py
│   │   ├── purchase.py
│   │   └── admin.py
│   ├── keyboards/
│   └── middlewares/
├── services/
│   ├── order_service.py         # Управление заказами, idempotency
│   ├── payment_service.py       # Бизнес-логика покупок
│   ├── user_service.py
│   ├── check_service.py
│   └── promo_service.py
├── db/
│   ├── models.py                # SQLAlchemy models
│   ├── session.py               # Async session factory
│   └── migrations/              # Alembic
├── core/
│   └── queue.py                 # Абстракция очереди
├── workers/
│   └── order_worker.py          # Обработка очереди заказов
├── api_clients/
│   └── fragment/
│       ├── __init__.py
│       ├── client.py            # Низкоуровневый Fragment client
│       ├── config.py            # FragmentConfig
│       └── exceptions.py        # Доменные исключения
└── utils/
    ├── logger.py
    └── money.py
```

## 3. Поток покупки Stars/Premium

```
1. Handler получает запрос на покупку
   │
2. OrderService.create_order() — создаёт заказ (PENDING)
   │  └── Idempotent: если order_key существует → возврат существующего
   │
3. OrderWorker.enqueue_order() — добавляет в очередь
   │
4. OrderWorker._process_order() — берёт из очереди
   │  └── Проверка идемпотентности: если COMPLETED → skip
   │  └── Устанавливает PROCESSING
   │
5. PaymentService.execute_order()
   │  └── FragmentClient.search_*_recipient()
   │  └── FragmentClient.init_*_request()
   │  └── FragmentClient.execute_*_payment()
   │
6. Результат:
   ├── SUCCESS → OrderService.set_completed() + credit_balance()
   ├── RETRY → OrderService.return_to_pending() + requeue()
   └── FAILED → OrderService.set_failed()
```

## 4. БД (основные таблицы)

| Таблица | Описание |
|---------|----------|
| users | Пользователи, балансы, реферер |
| orders | Заказы с idempotency key |
| transactions | История транзакций |
| payments | Внешние платежи |
| balance_ledger | Журнал изменений баланса |
| checks | Чеки |
| check_activations | Активации чеков |
| promo_codes | Промокоды |
| promo_uses | Использования промокодов |
| referral_earnings | Реферальные начисления |
| audit_logs | Аудит |
| settings | Настройки системы |

## 5. Гарантии безопасности

- **Idempotency**: `order_key` UNIQUE — повторный запрос не создаёт дубликат
- **Атомарность**: UPDATE user + INSERT ledger в одной транзакции
- **No double-spend**: Проверка статуса перед выполнением
- **Recovery**: Застрявшие заказы возвращаются в очередь при старте
- **Retry**: Exponential backoff для временных ошибок
- **Secrets**: .env не в git, валидация при старте

## 6. Компоненты и ответственность

| Компонент | Ответственность | НЕ делает |
|-----------|-----------------|-----------|
| **FragmentClient** | HTTP к Fragment, TON-транзакции | Не знает о заказах, БД |
| **PaymentService** | Оркестрация покупки, маппинг ошибок | Не управляет статусами |
| **OrderService** | CRUD заказов, idempotency, балансы | Не вызывает Fragment |
| **OrderWorker** | Обработка очереди, retry, recovery | Не содержит бизнес-логики |
| **Handlers** | Создание заказа, отправка в очередь | Не выполняют платежи |