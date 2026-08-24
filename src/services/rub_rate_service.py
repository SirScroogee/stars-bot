"""Helpers for displaying USDT amounts in RUB."""
import asyncio
import logging
import time
from decimal import Decimal, ROUND_HALF_UP

from src.services.platega_service import get_platega_usdt_rub_rate

logger = logging.getLogger(__name__)

RUB_RATE_CACHE_TTL_SECONDS = 60
PLATEGA_RATE_TIMEOUT_SECONDS = 3
CRYPTOBOT_RATE_TIMEOUT_SECONDS = 3

_rate_cache: dict[str, Decimal | str | float | None] = {
    "rate": None,
    "source": None,
    "expires_at": 0.0,
}
_rate_lock = asyncio.Lock()


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _format_usdt(amount_usdt: Decimal | int | float | str) -> str:
    return f"{_to_decimal(amount_usdt):,.2f} USDT"


def format_rub_amount(
    amount_usdt: Decimal | int | float | str,
    usdt_rub_rate: Decimal | int | float | str | None,
) -> str | None:
    """Return a RUB amount string for a USDT amount and RUB/USDT rate."""
    if usdt_rub_rate is None:
        return None

    amount = _to_decimal(amount_usdt)
    rate = _to_decimal(usdt_rub_rate)
    if rate <= 0:
        return None

    rub_amount = (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rub_amount:,.2f}"


def format_usdt_with_rub_from_rate(
    amount_usdt: Decimal | int | float | str,
    usdt_rub_rate: Decimal | int | float | str | None,
) -> str:
    """Format as '0.80 USDT (80.00 RUB)' when a RUB rate is available."""
    usdt_text = _format_usdt(amount_usdt)
    rub_text = format_rub_amount(amount_usdt, usdt_rub_rate)
    if not rub_text:
        return usdt_text
    return f"{usdt_text} ({rub_text} RUB)"


async def get_display_usdt_rub_rate() -> tuple[Decimal | None, str | None]:
    """Return RUB per 1 USDT for UI display.

    Platega is the primary source. CryptoBot is used when Platega is disabled
    or does not answer quickly enough.
    """
    now = time.monotonic()
    cached_rate = _rate_cache["rate"]
    cached_source = _rate_cache["source"]
    if cached_rate and float(_rate_cache["expires_at"] or 0) > now:
        return _to_decimal(cached_rate), str(cached_source) if cached_source else None

    async with _rate_lock:
        now = time.monotonic()
        cached_rate = _rate_cache["rate"]
        cached_source = _rate_cache["source"]
        if cached_rate and float(_rate_cache["expires_at"] or 0) > now:
            return _to_decimal(cached_rate), str(cached_source) if cached_source else None

        try:
            rate = await asyncio.wait_for(
                get_platega_usdt_rub_rate(),
                timeout=PLATEGA_RATE_TIMEOUT_SECONDS,
            )
            source = "platega"
        except Exception as platega_error:
            logger.warning("Could not get Platega USDT/RUB display rate: %s", platega_error)
            try:
                from src.services.cryptopay_service import get_usdt_rub_rate_from_cryptobot

                rate = await asyncio.wait_for(
                    get_usdt_rub_rate_from_cryptobot(),
                    timeout=CRYPTOBOT_RATE_TIMEOUT_SECONDS,
                )
                source = "cryptobot"
            except Exception as cryptobot_error:
                logger.warning(
                    "Could not get CryptoBot USDT/RUB display rate: %s",
                    cryptobot_error,
                )
                return None, None

        _rate_cache.update(
            {
                "rate": rate,
                "source": source,
                "expires_at": time.monotonic() + RUB_RATE_CACHE_TTL_SECONDS,
            }
        )
        return rate, source


async def format_usdt_with_rub(amount_usdt: Decimal | int | float | str) -> str:
    """Format a USDT amount with RUB in parentheses when a rate is available."""
    rate, _source = await get_display_usdt_rub_rate()
    return format_usdt_with_rub_from_rate(amount_usdt, rate)
