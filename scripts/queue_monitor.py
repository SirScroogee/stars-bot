"""
Мониторинг очереди заказов в реальном времени.

Запуск:
    python scripts/queue_monitor.py

    # С интервалом 5 секунд
    python scripts/queue_monitor.py --interval 5

    # Однократный вывод
    python scripts/queue_monitor.py --once
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import get_config


async def get_redis_stats(redis_url: str) -> dict:
    """Получить статистику Redis очереди."""
    try:
        import redis.asyncio as redis
    except ImportError:
        return {"error": "redis package not installed"}

    try:
        client = redis.from_url(redis_url)

        # Получаем размеры очередей
        main_queue_size = await client.llen("orders:queue")
        delayed_queue_size = await client.zcard("orders:delayed")
        processing_count = await client.hlen("orders:processing")

        # Получаем элементы в обработке
        processing_items = await client.hgetall("orders:processing")

        # Получаем delayed элементы с их временем
        delayed_items = await client.zrange("orders:delayed", 0, -1, withscores=True)

        # Общая информация о Redis
        info = await client.info("memory")

        await client.close()

        return {
            "main_queue_size": main_queue_size,
            "delayed_queue_size": delayed_queue_size,
            "processing_count": processing_count,
            "processing_items": processing_items,
            "delayed_items": delayed_items,
            "memory_used": info.get("used_memory_human", "N/A"),
            "memory_peak": info.get("used_memory_peak_human", "N/A"),
        }
    except Exception as e:
        return {"error": str(e)}


async def get_db_stats() -> dict:
    """Получить статистику заказов из БД."""
    try:
        from sqlalchemy import func, select
        from src.db.session import async_session_factory
        from src.db.models import Order
    except ImportError as e:
        return {"error": f"Import error: {e}"}

    try:
        async with async_session_factory() as session:
            # Считаем заказы по статусам
            result = await session.execute(
                select(Order.status, func.count(Order.id))
                .group_by(Order.status)
            )
            status_counts = dict(result.all())

            # Последние 5 заказов
            result = await session.execute(
                select(Order)
                .order_by(Order.created_at.desc())
                .limit(5)
            )
            recent_orders = result.scalars().all()

            return {
                "status_counts": status_counts,
                "recent_orders": [
                    {
                        "id": o.id,
                        "order_key": o.order_key,
                        "status": o.status,
                        "product_type": o.product_type,
                        "quantity": o.quantity,
                        "created_at": o.created_at.isoformat() if o.created_at else None,
                    }
                    for o in recent_orders
                ],
            }
    except Exception as e:
        return {"error": str(e)}


def format_stats(redis_stats: dict, db_stats: dict) -> str:
    """Форматировать статистику для вывода."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    output = f"""
╔══════════════════════════════════════════════════════════════════════╗
║             МОНИТОРИНГ ОЧЕРЕДИ ЗАКАЗОВ                               ║
║             {now}                                      ║
╠══════════════════════════════════════════════════════════════════════╣
"""

    # Redis статистика
    if "error" in redis_stats:
        output += f"║  ❌ Redis: {redis_stats['error']:<56} ║\n"
    else:
        output += f"""║  📊 REDIS QUEUE                                                      ║
║  ──────────────────────────────────────────────────────────────────  ║
║  Основная очередь:      {redis_stats['main_queue_size']:>8} заказов                        ║
║  Отложенная очередь:    {redis_stats['delayed_queue_size']:>8} заказов                        ║
║  В обработке:           {redis_stats['processing_count']:>8} заказов                        ║
║  Память Redis:          {redis_stats['memory_used']:>8}                               ║
║  Пик памяти:            {redis_stats['memory_peak']:>8}                               ║
"""

        if redis_stats['processing_items']:
            output += "║                                                                      ║\n"
            output += "║  🔄 В ОБРАБОТКЕ:                                                     ║\n"
            for order_id, data in list(redis_stats['processing_items'].items())[:5]:
                output += f"║     Order #{order_id.decode() if isinstance(order_id, bytes) else order_id:<15}                                      ║\n"

    output += "╠══════════════════════════════════════════════════════════════════════╣\n"

    # DB статистика
    if "error" in db_stats:
        output += f"║  ❌ Database: {db_stats['error']:<53} ║\n"
    else:
        output += "║  📦 ЗАКАЗЫ В БД                                                      ║\n"
        output += "║  ──────────────────────────────────────────────────────────────────  ║\n"

        status_emoji = {
            "pending": "⏳",
            "processing": "🔄",
            "completed": "✅",
            "failed": "❌",
            "cancelled": "🚫",
            "refunded": "💰",
        }

        for status, count in db_stats.get('status_counts', {}).items():
            emoji = status_emoji.get(status, "❓")
            output += f"║  {emoji} {status:<20} {count:>8}                                  ║\n"

        if db_stats.get('recent_orders'):
            output += "║                                                                      ║\n"
            output += "║  📋 ПОСЛЕДНИЕ ЗАКАЗЫ:                                                ║\n"
            for order in db_stats['recent_orders']:
                status_icon = status_emoji.get(order['status'], "❓")
                output += f"║     {status_icon} #{order['order_key']:<10} {order['product_type']:<8} {order['quantity']:>5} {order['status']:<12} ║\n"

    output += "╚══════════════════════════════════════════════════════════════════════╝\n"

    return output


async def monitor_loop(interval: float, once: bool = False):
    """Основной цикл мониторинга."""
    config = get_config()

    while True:
        # Очищаем экран (только если не однократный запуск)
        if not once:
            os.system('cls' if os.name == 'nt' else 'clear')

        # Собираем статистику
        redis_stats = await get_redis_stats(config.redis_url) if config.redis_url else {"error": "Redis URL не настроен"}
        db_stats = await get_db_stats()

        # Выводим
        print(format_stats(redis_stats, db_stats))

        if once:
            break

        print(f"  Обновление каждые {interval} сек. Нажмите Ctrl+C для выхода.")
        await asyncio.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Мониторинг очереди заказов")
    parser.add_argument("--interval", type=float, default=2.0, help="Интервал обновления (сек)")
    parser.add_argument("--once", action="store_true", help="Однократный вывод")
    args = parser.parse_args()

    try:
        asyncio.run(monitor_loop(args.interval, args.once))
    except KeyboardInterrupt:
        print("\n\n  Мониторинг остановлен.")


if __name__ == "__main__":
    main()
