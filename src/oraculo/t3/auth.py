from __future__ import annotations

import asyncio
import hashlib
import json
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from keyring.backends.macOS import Keyring
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)


class AuthRequired(RuntimeError):
    pass


def validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("MCP endpoint must not contain credentials, query or fragment")
    if not parsed.hostname or (
        parsed.scheme != "https"
        and not (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        raise ValueError("MCP endpoint requires HTTPS or loopback HTTP")


class KeychainStorage:
    def __init__(
        self,
        endpoint: str,
        *,
        backend: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        validate_endpoint(endpoint)
        self.service = "Oraculo.OAuth." + hashlib.sha256(endpoint.encode()).hexdigest()
        self.backend = backend if backend is not None else Keyring()
        self.clock = clock
        self.expires_at: float | None = None

    async def _get(self, key: str) -> dict[str, Any] | None:
        try:
            value = await asyncio.to_thread(
                self.backend.get_password, self.service, key
            )
            return json.loads(value) if value else None
        except Exception as exc:
            raise AuthRequired(
                "OAuth Keychain unavailable or invalid; run oraculo auth login"
            ) from exc

    async def _set(self, key: str, value: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(
                self.backend.set_password, self.service, key, json.dumps(value)
            )
        except Exception as exc:
            raise AuthRequired(
                "Unable to persist OAuth credentials in Keychain"
            ) from exc

    async def get_tokens(self) -> OAuthToken | None:
        stored = await self._get("tokens")
        if stored is None:
            return None
        try:
            token = OAuthToken.model_validate(stored["token"])
            self.expires_at = stored["expires_at"]
            if self.expires_at is not None:
                token = token.model_copy(
                    update={"expires_in": max(0, int(self.expires_at - self.clock()))}
                )
            return token
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthRequired(
                "Stored OAuth record is invalid; run oraculo auth login"
            ) from exc

    async def set_tokens(self, tokens: OAuthToken) -> None:
        received_at = self.clock()
        self.expires_at = (
            received_at + tokens.expires_in if tokens.expires_in is not None else None
        )
        await self._set(
            "tokens",
            {
                "token": tokens.model_dump(mode="json"),
                "received_at": received_at,
                "expires_at": self.expires_at,
            },
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        stored = await self._get("client")
        try:
            return OAuthClientInformationFull.model_validate(stored) if stored else None
        except ValueError as exc:
            raise AuthRequired(
                "Stored OAuth client is invalid; run oraculo auth login"
            ) from exc

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._set("client", client_info.model_dump(mode="json"))


class PersistentOAuthProvider(OAuthClientProvider):
    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        if isinstance(storage, KeychainStorage):
            self.context.token_expiry_time = storage.expires_at
            saved = await storage._get("discovery")
            if saved:
                try:
                    self.context.oauth_metadata = (
                        OAuthMetadata.model_validate(saved["oauth"])
                        if saved.get("oauth")
                        else None
                    )
                    self.context.protected_resource_metadata = (
                        ProtectedResourceMetadata.model_validate(saved["resource"])
                        if saved.get("resource")
                        else None
                    )
                    self.context.auth_server_url = saved.get("auth_server_url")
                    if self.context.protected_resource_metadata:
                        await self._validate_resource_match(
                            self.context.protected_resource_metadata
                        )
                except (ValueError, KeyError, TypeError) as exc:
                    raise AuthRequired(
                        "Stored OAuth discovery is invalid; run oraculo auth login"
                    ) from exc

    async def _persist_discovery(self) -> None:
        storage = self.context.storage
        if isinstance(storage, KeychainStorage):
            await storage._set(
                "discovery",
                {
                    "oauth": self.context.oauth_metadata.model_dump(mode="json")
                    if self.context.oauth_metadata
                    else None,
                    "resource": self.context.protected_resource_metadata.model_dump(
                        mode="json"
                    )
                    if self.context.protected_resource_metadata
                    else None,
                    "auth_server_url": self.context.auth_server_url,
                },
            )

    async def _handle_token_response(self, response: httpx2.Response) -> None:
        await self._persist_discovery()
        await super()._handle_token_response(response)


async def _no_redirect(url: str) -> None:
    raise AuthRequired("T3 OAuth requires interactive login: oraculo auth login")


async def _no_callback() -> AuthorizationCodeResult:
    raise AuthRequired("T3 OAuth requires interactive login: oraculo auth login")


def build_auth(
    endpoint: str, *, storage: KeychainStorage | None = None
) -> PersistentOAuthProvider:
    validate_endpoint(endpoint)
    return PersistentOAuthProvider(
        server_url=endpoint,
        client_metadata=OAuthClientMetadata(
            client_name="Oraculo",
            redirect_uris=["http://127.0.0.1:8769/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage or KeychainStorage(endpoint),
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )


async def login(
    endpoint: str, *, storage: KeychainStorage | None = None, timeout: float = 180
) -> None:
    from oraculo.t3.client import T3Client

    auth = build_auth(endpoint, storage=storage)
    future: asyncio.Future[AuthorizationCodeResult] = (
        asyncio.get_running_loop().create_future()
    )
    expected_state: str | None = None

    async def receive(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = "400 Bad Request"
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            method, target, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
            if (
                method == "GET"
                and parsed.path == "/callback"
                and expected_state
                and query.get("state") == [expected_state]
            ):
                relevant = {
                    "code",
                    "state",
                    "iss",
                    "error",
                    "error_description",
                    "error_uri",
                }
                valid = all(
                    len(values) == 1 and 0 < len(values[0]) <= 4096
                    for key, values in query.items()
                    if key in relevant
                )
                if (
                    valid
                    and "error" in query
                    and "code" not in query
                    and not future.done()
                ):
                    future.set_exception(
                        AuthRequired("OAuth authorization was denied or failed")
                    )
                elif (
                    valid
                    and "code" in query
                    and "error" not in query
                    and not future.done()
                ):
                    future.set_result(
                        AuthorizationCodeResult(
                            code=query["code"][0],
                            state=expected_state,
                            iss=query.get("iss", [None])[0],
                        )
                    )
                    status = "200 OK"
        except (
            ValueError,
            UnicodeError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            body = b"Return to Oraculo."
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    async def redirect(url: str) -> None:
        nonlocal expected_state
        values = parse_qs(urlsplit(url).query)
        expected_state = values.get("state", [None])[0]
        if not expected_state:
            raise AuthRequired("Authorization URL missing state")
        if not await asyncio.to_thread(webbrowser.open, url):
            raise AuthRequired("Could not open browser for OAuth login")

    async def callback() -> AuthorizationCodeResult:
        return await asyncio.wait_for(asyncio.shield(future), timeout)

    auth.context.redirect_handler = redirect
    auth.context.callback_handler = callback
    try:
        server = await asyncio.start_server(receive, "127.0.0.1", 8769, limit=8192)
    except OSError as exc:
        raise AuthRequired("OAuth callback port 8769 unavailable") from exc
    async with server:
        async with asyncio.timeout(timeout):
            async with T3Client(endpoint, auth=auth):
                pass
