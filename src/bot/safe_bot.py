"""
SafeBot — обёртка для Bot с фильтрацией исходящих сообщений.
"""
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ForceReply,
)

from src.bot.callback_utils import is_message_not_modified_error

logger = logging.getLogger(__name__)

# Запрещённые фрагменты текста (никогда не отправляются)
BLOCKED_PATTERNS = [
    "+42777",
]


def _contains_blocked_pattern(text: str | None) -> bool:
    """Проверить, содержит ли текст запрещённый паттерн."""
    if not text:
        return False
    for pattern in BLOCKED_PATTERNS:
        if pattern in text:
            return True
    return False


def _sanitize_text(text: str | None) -> str | None:
    """Удалить запрещённые паттерны из текста."""
    if not text:
        return text
    result = text
    for pattern in BLOCKED_PATTERNS:
        result = result.replace(pattern, "[BLOCKED]")
    return result


class SafeBot(Bot):
    """
    Bot с фильтрацией исходящих сообщений.

    Все сообщения проверяются на наличие запрещённых паттернов.
    Если паттерн найден — он заменяется на [BLOCKED].
    """

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        entities: list | None = None,
        link_preview_options: Any = None,
        disable_notification: bool | None = None,
        protect_content: bool | None = None,
        reply_parameters: Any = None,
        reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | ForceReply | None = None,
        allow_sending_without_reply: bool | None = None,
        disable_web_page_preview: bool | None = None,
        reply_to_message_id: int | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить сообщение с фильтрацией."""
        if _contains_blocked_pattern(text):
            logger.warning(f"Blocked pattern detected in send_message to {chat_id}")
            text = _sanitize_text(text)

        return await super().send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=message_thread_id,
            parse_mode=parse_mode,
            entities=entities,
            link_preview_options=link_preview_options,
            disable_notification=disable_notification,
            protect_content=protect_content,
            reply_parameters=reply_parameters,
            reply_markup=reply_markup,
            allow_sending_without_reply=allow_sending_without_reply,
            disable_web_page_preview=disable_web_page_preview,
            reply_to_message_id=reply_to_message_id,
            **kwargs,
        )

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | str | None = None,
        message_id: int | None = None,
        inline_message_id: str | None = None,
        *,
        parse_mode: str | None = None,
        entities: list | None = None,
        link_preview_options: Any = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        disable_web_page_preview: bool | None = None,
        **kwargs: Any,
    ) -> Message | bool:
        """Редактировать сообщение с фильтрацией."""
        if _contains_blocked_pattern(text):
            logger.warning(f"Blocked pattern detected in edit_message_text")
            text = _sanitize_text(text)

        try:
            return await super().edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                inline_message_id=inline_message_id,
                parse_mode=parse_mode,
                entities=entities,
                link_preview_options=link_preview_options,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
                **kwargs,
            )
        except TelegramBadRequest as exc:
            if is_message_not_modified_error(exc):
                logger.debug("Ignoring an idempotent message edit")
                return True
            raise

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool | None = None,
        url: str | None = None,
        cache_time: int | None = None,
        **kwargs: Any,
    ) -> bool:
        """Ответить на callback с фильтрацией."""
        if _contains_blocked_pattern(text):
            logger.warning(f"Blocked pattern detected in answer_callback_query")
            text = _sanitize_text(text)

        return await super().answer_callback_query(
            callback_query_id=callback_query_id,
            text=text,
            show_alert=show_alert,
            url=url,
            cache_time=cache_time,
            **kwargs,
        )

    async def send_photo(
        self,
        chat_id: int | str,
        photo: Any,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить фото с фильтрацией caption."""
        if _contains_blocked_pattern(caption):
            logger.warning(f"Blocked pattern detected in send_photo caption")
            caption = _sanitize_text(caption)

        return await super().send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            **kwargs,
        )

    async def send_document(
        self,
        chat_id: int | str,
        document: Any,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить документ с фильтрацией caption."""
        if _contains_blocked_pattern(caption):
            logger.warning(f"Blocked pattern detected in send_document caption")
            caption = _sanitize_text(caption)

        return await super().send_document(
            chat_id=chat_id,
            document=document,
            caption=caption,
            **kwargs,
        )

    async def send_video(
        self,
        chat_id: int | str,
        video: Any,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить видео с фильтрацией caption."""
        if _contains_blocked_pattern(caption):
            logger.warning(f"Blocked pattern detected in send_video caption")
            caption = _sanitize_text(caption)

        return await super().send_video(
            chat_id=chat_id,
            video=video,
            caption=caption,
            **kwargs,
        )

    async def send_animation(
        self,
        chat_id: int | str,
        animation: Any,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить анимацию с фильтрацией caption."""
        if _contains_blocked_pattern(caption):
            logger.warning(f"Blocked pattern detected in send_animation caption")
            caption = _sanitize_text(caption)

        return await super().send_animation(
            chat_id=chat_id,
            animation=animation,
            caption=caption,
            **kwargs,
        )

    async def send_voice(
        self,
        chat_id: int | str,
        voice: Any,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить голосовое с фильтрацией caption."""
        if _contains_blocked_pattern(caption):
            logger.warning(f"Blocked pattern detected in send_voice caption")
            caption = _sanitize_text(caption)

        return await super().send_voice(
            chat_id=chat_id,
            voice=voice,
            caption=caption,
            **kwargs,
        )

    async def send_audio(
        self,
        chat_id: int | str,
        audio: Any,
        *,
        caption: str | None = None,
        **kwargs: Any,
    ) -> Message:
        """Отправить аудио с фильтрацией caption."""
        if _contains_blocked_pattern(caption):
            logger.warning(f"Blocked pattern detected in send_audio caption")
            caption = _sanitize_text(caption)

        return await super().send_audio(
            chat_id=chat_id,
            audio=audio,
            caption=caption,
            **kwargs,
        )

    async def answer_inline_query(
        self,
        inline_query_id: str,
        results: list,
        *,
        cache_time: int | None = None,
        is_personal: bool | None = None,
        next_offset: str | None = None,
        button: Any = None,
        **kwargs: Any,
    ) -> bool:
        """Ответить на inline query с фильтрацией результатов."""
        # Фильтруем результаты
        filtered_results = []
        for result in results:
            # Проверяем title и description если есть
            if hasattr(result, 'title') and _contains_blocked_pattern(result.title):
                logger.warning(f"Blocked pattern detected in inline result title")
                result.title = _sanitize_text(result.title)
            if hasattr(result, 'description') and _contains_blocked_pattern(result.description):
                logger.warning(f"Blocked pattern detected in inline result description")
                result.description = _sanitize_text(result.description)
            # Проверяем input_message_content если есть
            if hasattr(result, 'input_message_content') and result.input_message_content:
                content = result.input_message_content
                if hasattr(content, 'message_text') and _contains_blocked_pattern(content.message_text):
                    logger.warning(f"Blocked pattern detected in inline result message_text")
                    content.message_text = _sanitize_text(content.message_text)
            filtered_results.append(result)

        return await super().answer_inline_query(
            inline_query_id=inline_query_id,
            results=filtered_results,
            cache_time=cache_time,
            is_personal=is_personal,
            next_offset=next_offset,
            button=button,
            **kwargs,
        )
