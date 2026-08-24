from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from src.bot.callback_utils import is_stale_callback_error, safe_callback_answer


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


def test_only_expired_callback_errors_are_filtered():
    assert is_stale_callback_error(_bad_request("Bad Request: query is too old"))
    assert is_stale_callback_error(_bad_request("Bad Request: query ID is invalid"))
    assert not is_stale_callback_error(_bad_request("Bad Request: chat not found"))
    assert not is_stale_callback_error(RuntimeError("query is too old"))


@pytest.mark.asyncio
async def test_safe_callback_answer_swallows_only_expired_query():
    callback = SimpleNamespace(
        answer=AsyncMock(side_effect=_bad_request("Bad Request: query is too old")),
        from_user=SimpleNamespace(id=1001),
    )
    assert not await safe_callback_answer(callback)

    callback.answer.side_effect = _bad_request("Bad Request: chat not found")
    with pytest.raises(TelegramBadRequest):
        await safe_callback_answer(callback)
