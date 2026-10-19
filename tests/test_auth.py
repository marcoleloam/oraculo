import json

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from oraculo.t3.auth import AuthRequired, KeychainStorage, build_auth, validate_endpoint


class Keychain:
    def __init__(self):
        self.values = {}

    def get_password(self, service, key):
        return self.values.get((service, key))

    def set_password(self, service, key, value):
        self.values[service, key] = value


async def test_expiry_survives_restart():
    backend = Keychain()
    now = [100.0]
    storage = KeychainStorage(
        "https://example.com/mcp", backend=backend, clock=lambda: now[0]
    )
    await storage.set_tokens(
        OAuthToken(
            access_token="test-only",
            token_type="Bearer",
            expires_in=60,
            refresh_token="refresh-only",
        )
    )
    now[0] = 130
    restarted = KeychainStorage(
        "https://example.com/mcp", backend=backend, clock=lambda: now[0]
    )
    assert (await restarted.get_tokens()).expires_in == 30
    now[0] = 170
    assert (await restarted.get_tokens()).expires_in == 0
    assert restarted.expires_at == 160
    provider = build_auth("https://example.com/mcp", storage=restarted)
    await provider._initialize()
    assert provider.context.token_expiry_time == 160
    assert not provider.context.is_token_valid()
    assert json.loads(backend.values[(storage.service, "tokens")])["expires_at"] == 160


async def test_client_info_roundtrip():
    storage = KeychainStorage("https://example.com/mcp", backend=Keychain())
    client = OAuthClientInformationFull(
        client_id="test-client",
        redirect_uris=["http://127.0.0.1:8769/callback"],
        token_endpoint_auth_method="none",
    )
    await storage.set_client_info(client)
    assert (await storage.get_client_info()).client_id == "test-client"


async def test_background_auth_never_opens_browser(monkeypatch):
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda url: calls.append(url))
    auth = build_auth(
        "https://example.com/mcp",
        storage=KeychainStorage("https://example.com/mcp", backend=Keychain()),
    )
    with pytest.raises(AuthRequired):
        await auth.context.redirect_handler("https://example.com/authorize")
    with pytest.raises(AuthRequired):
        await auth.context.callback_handler()
    assert calls == []


async def test_locked_keychain_requires_auth():
    class Locked(Keychain):
        def get_password(self, service, key):
            raise RuntimeError("locked")

    storage = KeychainStorage("https://example.com/mcp", backend=Locked())
    with pytest.raises(AuthRequired, match="Keychain"):
        await storage.get_tokens()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.example/mcp",
        "https://user:token@example.com/mcp",
        "https://example.com/mcp?token=x",
    ],
)
def test_endpoint_never_embeds_credentials(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


async def test_refresh_rotation_persisted_and_serialized():
    import asyncio
    import time

    import httpx2
    from mcp.shared.auth import OAuthMetadata

    backend = Keychain()
    endpoint = "https://example.com/mcp"
    storage = KeychainStorage(endpoint, backend=backend)
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="client",
            redirect_uris=["http://127.0.0.1:8769/callback"],
            token_endpoint_auth_method="none",
        )
    )
    await storage.set_tokens(
        OAuthToken(
            access_token="old-test",
            token_type="Bearer",
            expires_in=0,
            refresh_token="refresh-test",
        )
    )
    auth = build_auth(endpoint, storage=storage)
    auth.context.oauth_metadata = OAuthMetadata(
        issuer="https://example.com",
        authorization_endpoint="https://example.com/auth",
        token_endpoint="https://example.com/oauth/token",
        response_types_supported=["code"],
    )
    await auth._persist_discovery()
    restarted = build_auth(endpoint, storage=KeychainStorage(endpoint, backend=backend))
    refreshes = []

    async def handle(request):
        if request.url.path == "/oauth/token":
            refreshes.append(request)
            await asyncio.sleep(0.01)
            return httpx2.Response(
                200,
                json={
                    "access_token": "new-test",
                    "refresh_token": "rotated-test",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert request.headers["Authorization"] == "Bearer new-test"
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(
        auth=restarted, transport=httpx2.MockTransport(handle)
    ) as client:
        await asyncio.gather(client.get(endpoint), client.get(endpoint))
    assert len(refreshes) == 1
    tokens = await storage.get_tokens()
    assert tokens.refresh_token == "rotated-test"
    assert storage.expires_at > time.time() + 3500


@pytest.mark.parametrize(
    "issuer", ["https://example.com", None, "https://wrong.example"]
)
async def test_login_loopback_preserves_sdk_issuer_validation(monkeypatch, issuer):
    import asyncio
    from urllib.parse import parse_qs, urlencode, urlsplit

    from mcp.client.auth.exceptions import OAuthFlowError
    from mcp.shared.auth import OAuthMetadata

    from oraculo.t3.auth import login

    loop = asyncio.get_running_loop()
    original_start_server = asyncio.start_server
    bound_port = []
    responses = []

    async def ephemeral_server(handler, host, port, **kwargs):
        server = await original_start_server(handler, host, 0, **kwargs)
        bound_port.append(server.sockets[0].getsockname()[1])
        return server

    async def callback(query):
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port[0])
        writer.write(
            (
                "GET /callback?"
                + urlencode(query, doseq=True)
                + " HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        responses.append(await reader.read())
        writer.close()
        await writer.wait_closed()

    def browser(url):
        state = parse_qs(urlsplit(url).query)["state"][0]
        query = {"state": state, "code": "test-code"}
        if issuer is not None:
            query["iss"] = issuer
        asyncio.run_coroutine_threadsafe(callback(query), loop).result(timeout=2)
        return True

    class RemoteClient:
        def __init__(self, endpoint, auth):
            self.auth = auth

        async def __aenter__(self):
            self.auth.context.oauth_metadata = OAuthMetadata(
                issuer="https://example.com",
                authorization_endpoint="https://example.com/auth",
                token_endpoint="https://example.com/oauth/token",
                response_types_supported=["code"],
                authorization_response_iss_parameter_supported=True,
            )
            self.auth.context.client_info = OAuthClientInformationFull(
                client_id="test", redirect_uris=["http://127.0.0.1:8769/callback"]
            )
            code, verifier = await self.auth._perform_authorization_code_grant()
            assert code == "test-code"
            assert verifier
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(asyncio, "start_server", ephemeral_server)
    monkeypatch.setattr("webbrowser.open", browser)
    monkeypatch.setattr("oraculo.t3.client.T3Client", RemoteClient)
    if issuer == "https://example.com":
        await login(
            "https://example.com/mcp",
            storage=KeychainStorage("https://example.com/mcp", backend=Keychain()),
            timeout=3,
        )
    else:
        with pytest.raises(OAuthFlowError):
            await login(
                "https://example.com/mcp",
                storage=KeychainStorage("https://example.com/mcp", backend=Keychain()),
                timeout=3,
            )
    assert responses[0].startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize(
    "bad",
    [
        {"iss": ["https://example.com", "https://other.example"]},
        {"code": "x" * 4097},
        {"code": "", "error": "denied"},
        {"state": ["state", "state"]},
    ],
)
async def test_login_callback_rejects_invalid_query_then_accepts_valid(
    monkeypatch, bad
):
    import asyncio
    from urllib.parse import urlencode

    from oraculo.t3.auth import login

    original_start_server = asyncio.start_server
    bound_port = []

    async def ephemeral_server(handler, host, port, **kwargs):
        server = await original_start_server(handler, host, 0, **kwargs)
        bound_port.append(server.sockets[0].getsockname()[1])
        return server

    async def callback(query):
        reader, writer = await asyncio.open_connection("127.0.0.1", bound_port[0])
        writer.write(
            (
                "GET /callback?"
                + urlencode(query, doseq=True)
                + " HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    class RemoteClient:
        def __init__(self, endpoint, auth):
            self.auth = auth

        async def __aenter__(self):
            await self.auth.context.redirect_handler(
                "https://example.com/auth?state=state"
            )
            invalid = await callback({"state": "state", "code": "test", **bad})
            assert invalid.startswith(b"HTTP/1.1 400")
            valid = await callback(
                {"state": "state", "code": "test", "iss": "https://example.com"}
            )
            assert valid.startswith(b"HTTP/1.1 200")
            result = await self.auth.context.callback_handler()
            assert result.iss == "https://example.com"
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(asyncio, "start_server", ephemeral_server)
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    monkeypatch.setattr("oraculo.t3.client.T3Client", RemoteClient)
    await login(
        "https://example.com/mcp",
        storage=KeychainStorage("https://example.com/mcp", backend=Keychain()),
        timeout=3,
    )


async def test_callback_remote_denial_ends_login_without_exposing_description(
    monkeypatch,
):
    import asyncio

    from oraculo.t3.auth import login

    original = asyncio.start_server
    ports = []

    async def bind(handler, host, port, **kwargs):
        server = await original(handler, host, 0, **kwargs)
        ports.append(server.sockets[0].getsockname()[1])
        return server

    class RemoteClient:
        def __init__(self, endpoint, auth):
            self.auth = auth

        async def __aenter__(self):
            await self.auth.context.redirect_handler(
                "https://example.com/auth?state=expected"
            )
            reader, writer = await asyncio.open_connection("127.0.0.1", ports[0])
            writer.write(
                b"GET /callback?state=expected&error=access_denied&error_description=private-data HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            await writer.drain()
            assert (await reader.read()).startswith(b"HTTP/1.1 400")
            writer.close()
            await writer.wait_closed()
            await self.auth.context.callback_handler()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(asyncio, "start_server", bind)
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    monkeypatch.setattr("oraculo.t3.client.T3Client", RemoteClient)
    with pytest.raises(AuthRequired, match="denied or failed") as error:
        await login(
            "https://example.com/mcp",
            storage=KeychainStorage("https://example.com/mcp", backend=Keychain()),
            timeout=3,
        )
    assert "private-data" not in str(error.value)
