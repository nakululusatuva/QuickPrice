from __future__ import annotations

import pytest

from quickprice.providers.base import (
    HttpProvider,
    MalformedResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)


class FakeResponse:
    def __init__(self, status: int, payload=None, error: Exception | None = None):
        self.status = status
        self.payload = payload
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, **kwargs):
        if self.error:
            raise self.error
        return self.payload


class FakeSession:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
        (401, ProviderError),
    ],
)
async def test_http_statuses_are_normalized_without_response_body(status, error_type):
    provider = HttpProvider(session=FakeSession(FakeResponse(status, {"secret": "never log"})))
    with pytest.raises(error_type) as caught:
        await provider._request_json("GET", "https://example.invalid?apikey=secret")
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_json_is_normalized():
    provider = HttpProvider(session=FakeSession(FakeResponse(200, error=ValueError("not json"))))
    with pytest.raises(MalformedResponse):
        await provider._request_json("GET", "https://example.invalid")


@pytest.mark.asyncio
async def test_disconnect_and_timeout_are_retryable():
    provider = HttpProvider(session=FakeSession(error=TimeoutError()))
    with pytest.raises(ProviderUnavailable) as caught:
        await provider._request_json("GET", "https://example.invalid")
    assert caught.value.retryable is True
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_http_proxy_is_applied_only_when_configured():
    proxied_session = FakeSession(FakeResponse(200, {}))
    proxied = HttpProvider(
        session=proxied_session,
        proxy_url="http://10.0.1.7:7890",
    )
    await proxied._request_json("GET", "https://example.invalid")

    direct_session = FakeSession(FakeResponse(200, {}))
    direct = HttpProvider(session=direct_session)
    await direct._request_json("GET", "https://example.invalid")

    assert proxied_session.calls[0][1]["proxy"] == "http://10.0.1.7:7890"
    assert "proxy" not in direct_session.calls[0][1]
