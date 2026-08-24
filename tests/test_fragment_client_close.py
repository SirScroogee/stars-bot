from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api_clients.fragment.client import FragmentClient


@pytest.mark.asyncio
async def test_fragment_client_close_releases_all_http_resources():
    client = object.__new__(FragmentClient)
    client._session = SimpleNamespace(closed=False, close=AsyncMock())
    client._connector = SimpleNamespace(closed=False, close=AsyncMock())
    client._ton_client = SimpleNamespace(close_session=AsyncMock())
    ton_client = client._ton_client

    await client.close()

    client._session.close.assert_awaited_once()
    client._connector.close.assert_awaited_once()
    ton_client.close_session.assert_awaited_once()
    assert client._ton_client is None
