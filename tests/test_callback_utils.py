from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from src.bot.callback_utils import (
    is_message_not_modified_error,
    is_stale_callback_error,
    safe_callback_answer,
)
from src.bot.safe_bot import SafeBot


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


def test_only_expired_callback_errors_are_filtered():
    assert is_stale_callback_error(_bad_request("Bad Request: query is too old"))
    assert is_stale_callback_error(_bad_request("Bad Request: query ID is invalid"))
    assert not is_stale_callback_error(_bad_request("Bad Request: chat not found"))
    assert not is_stale_callback_error(RuntimeError("query is too old"))


def test_only_idempotent_message_edit_errors_are_filtered():
    assert is_message_not_modified_error(
        _bad_request("Bad Request: message is not modified")
    )
    assert not is_message_not_modified_error(_bad_request("Bad Request: chat not found"))
    assert not is_message_not_modified_error(RuntimeError("message is not modified"))


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


@pytest.mark.asyncio
async def test_safe_bot_accepts_an_idempotent_message_edit():
    bot = object.__new__(SafeBot)
    error = _bad_request("Bad Request: message is not modified")

    with patch.object(Bot, "edit_message_text", AsyncMock(side_effect=error)):
        result = await SafeBot.edit_message_text(
            bot,
            "unchanged",
            chat_id=1001,
            message_id=55,
        )

    assert result is True
