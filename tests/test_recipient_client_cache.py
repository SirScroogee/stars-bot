from unittest.mock import AsyncMock

import pytest

from src.services import recipient_service


@pytest.mark.asyncio
async def test_clear_client_cache_closes_account_and_fallback_clients(monkeypatch):
    account_client = AsyncMock()
    fallback_client = AsyncMock()
    monkeypatch.setattr(recipient_service, "_fragment_clients", {7: account_client})
    monkeypatch.setattr(recipient_service, "_fallback_client", fallback_client)

    await recipient_service.clear_client_cache()

    account_client.close.assert_awaited_once()
    fallback_client.close.assert_awaited_once()
    assert recipient_service._fragment_clients == {}
    assert recipient_service._fallback_client is None
