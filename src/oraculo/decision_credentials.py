"""Bounded, lazy macOS Keychain access; never persist a credential or raw error."""

from __future__ import annotations

import asyncio
import sys

from oraculo.state import StateError

REFERENCE = "ref:oraculo-openai-decisions"
MAX_PASSWORD_BYTES = 8192


class CredentialUnavailable(StateError):
    def __init__(self, reason):
        self.reason = (
            reason
            if reason
            in {
                "invalid_value",
                "timeout",
                "access_denied_or_missing",
                "backend_unavailable",
                "invalid_reference",
                "unsupported_platform",
                "invalid_timeout",
                "cancelled",
            }
            else "backend_unavailable"
        )
        super().__init__("decision_credential_unavailable")


def valid_token(token):
    return (
        isinstance(token, str)
        and 0 < len(token.encode()) <= MAX_PASSWORD_BYTES
        and token.isascii()
        and all(33 <= ord(c) <= 126 for c in token)
    )


async def _security_password(timeout):
    process = None
    raw = bytearray()
    try:
        async with asyncio.timeout(timeout):
            # Only item identifiers are argv. No inherited environment, stdin,
            # shell, stderr, disk file, executor thread or Keyring backend.
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "oraculo.openai",
                "-a",
                "decisions-api",
                "-w",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},
                limit=MAX_PASSWORD_BYTES + 2,
            )
            while chunk := await process.stdout.read(1024):
                raw.extend(chunk)
                if len(raw) > MAX_PASSWORD_BYTES + 1:
                    raise CredentialUnavailable("invalid_value")
            code = await process.wait()
            if code:
                # security exit codes do not reliably distinguish ACL denial,
                # locked Keychain and missing item. Never inspect raw stderr.
                raise CredentialUnavailable("access_denied_or_missing")
            token = raw.decode("ascii").removesuffix("\n")
            if not valid_token(token):
                raise CredentialUnavailable("invalid_value")
            return token
    except TimeoutError:
        raise CredentialUnavailable("timeout") from None
    except asyncio.CancelledError:
        raise
    except CredentialUnavailable:
        raise
    except Exception:
        raise CredentialUnavailable("backend_unavailable") from None
    finally:
        raw.clear()
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # Reap a killed security process without waiting on an uncancellable
            # C call or the loop's default executor during daemon shutdown.
            try:
                await asyncio.wait_for(process.wait(), 0.25)
            except (TimeoutError, ProcessLookupError):
                pass


async def resolve_credential(reference, *, timeout=2.0):
    if reference != REFERENCE:
        raise CredentialUnavailable("invalid_reference")
    if sys.platform != "darwin":
        raise CredentialUnavailable("unsupported_platform")
    if type(timeout) not in {int, float} or not 0 < timeout <= 2:
        raise CredentialUnavailable("invalid_timeout")
    return await _security_password(timeout)
