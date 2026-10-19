from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from oraculo.config import Config
from oraculo.state import State


@dataclass(frozen=True, slots=True)
class Target:
    provider: str
    model: str
    profile: str

    @property
    def instance_id(self) -> str:
        return self.provider


class Router:
    def __init__(self, config: Config, state: State | None = None) -> None:
        self.config = config
        self.state = state
        self.blocked: dict[str, float] = {}
        self.last_reason = "unconfigured"

    def mark_quota(self, provider: str, now: float | None = None) -> None:
        until = (
            now if now is not None else time.time()
        ) + self.config.quota_backoff_seconds
        self.blocked[provider] = until
        if self.state:
            self.state.connection.execute(
                "INSERT INTO provider_eligibility(provider,blocked_until,reason) VALUES(?,?,'quota') ON CONFLICT(provider) DO UPDATE SET blocked_until=excluded.blocked_until,reason=excluded.reason",
                (provider, until),
            )

    def select(
        self,
        kind: str,
        catalog: dict[str, Any] | list[dict[str, Any]],
        now: float | None = None,
    ) -> Target | None:
        timestamp = time.time() if now is None else now
        entries = catalog.get("providers", []) if isinstance(catalog, dict) else catalog
        if isinstance(entries, dict):
            entries = [
                dict(value, providerInstanceId=key) for key, value in entries.items()
            ]
        live = {
            entry.get("providerInstanceId", entry.get("id")): entry for entry in entries
        }
        self.last_reason = "unavailable"
        for name in self.config.profiles.get(
            kind, self.config.profiles["implementation"]
        ):
            config = self.config.providers.get(name)
            if not config or not config.billing_verified or not config.models:
                continue
            provider = live.get(config.instance_id)
            if (
                not provider
                or provider.get("available") is False
                or provider.get("enabled") is False
                or provider.get("canRunChildTask") is False
                or bool(provider.get("constraints"))
                or provider.get("status")
                in {"unavailable", "auth_required", "disabled"}
            ):
                continue
            blocked = self.blocked.get(config.instance_id, 0)
            if self.state:
                row = self.state._one(
                    "SELECT blocked_until FROM provider_eligibility WHERE provider=?",
                    (config.instance_id,),
                )
                blocked = max(blocked, row["blocked_until"] if row else 0)
            if blocked > timestamp:
                self.last_reason = "quota_wait"
                continue
            models = provider.get("models", [])
            eligible = {
                m if isinstance(m, str) else m.get("id", m.get("model"))
                for m in models
                if isinstance(m, str)
                or (
                    m.get("available", True)
                    and not m.get("requiresAdditionalSpend", False)
                )
            }
            for model in config.models:
                if model in eligible:
                    self.last_reason = "ready"
                    return Target(config.instance_id, model, kind)
        return None
