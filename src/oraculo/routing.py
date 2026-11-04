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
        self.last_blockers: list[dict] = []

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
        *,
        reserved: dict[str, int] | None = None,
    ) -> Target | None:
        choices = self.candidates(kind, catalog, now, reserved=reserved)
        return choices[0] if choices else None

    def candidates(
        self, kind, catalog, now=None, *, reserved=None, explicit=None
    ) -> list[Target]:
        """Only subscription-authorized, available models; no model authority."""
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
        self.last_blockers = []
        if explicit is not None:
            # A user-pinned or inherited model is never filtered by the
            # config allowlist/profiles: those only drive automatic ranking.
            return self._explicit(kind, live, timestamp, reserved, explicit)
        choices = []
        for name in self.config.profiles.get(
            kind, self.config.profiles["implementation"]
        ):
            config = self.config.providers.get(name)
            if not config or not config.billing_verified or not config.models:
                self.last_blockers.append(
                    {
                        "provider": name,
                        "reason": "subscription_or_models_not_authorized",
                    }
                )
                continue
            provider = live.get(config.instance_id)
            if self._gate(config.instance_id, provider, timestamp, reserved):
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
                    candidate = Target(config.instance_id, model, kind)
                    if candidate not in choices:
                        choices.append(candidate)
            if not any(t.provider == config.instance_id for t in choices):
                self.last_blockers.append(
                    {
                        "provider": config.instance_id,
                        "reason": "authorized_model_unavailable",
                    }
                )
        if choices:
            self.last_reason = "ready"
        return choices

    def _gate(self, instance_id, provider, timestamp, reserved) -> str | None:
        """Host availability, quota and capacity gates; returns a blocker reason."""
        if (
            not provider
            or provider.get("available") is False
            or provider.get("enabled") is False
            or provider.get("canRunChildTask") is False
            or bool(provider.get("constraints"))
            or provider.get("status") in {"unavailable", "auth_required", "disabled"}
        ):
            self.last_blockers.append(
                {
                    "provider": instance_id,
                    "reason": "catalog_unavailable_or_constrained",
                    "constraints": provider.get("constraints", []) if provider else [],
                }
            )
            return "catalog_unavailable_or_constrained"
        blocked = self.blocked.get(instance_id, 0)
        if self.state:
            row = self.state._one(
                "SELECT blocked_until FROM provider_eligibility WHERE provider=?",
                (instance_id,),
            )
            blocked = max(blocked, row["blocked_until"] if row else 0)
        if blocked > timestamp:
            self.last_reason = "quota_wait"
            self.last_blockers.append(
                {"provider": instance_id, "reason": "quota_wait", "retry_at": blocked}
            )
            return "quota_wait"
        if self.state and not self.state.provider_available(
            instance_id, (reserved or {}).get(instance_id, 0) + 1
        ):
            self.last_reason = "provider_capacity"
            self.last_blockers.append(
                {"provider": instance_id, "reason": "provider_capacity"}
            )
            return "provider_capacity"
        return None

    def _explicit(self, kind, live, timestamp, reserved, explicit) -> list[Target]:
        """Honor an explicit/inherited target if the host catalog offers it."""

        def ids(entry):
            return [
                m if isinstance(m, str) else m.get("id", m.get("model"))
                for m in entry.get("models", [])
                if isinstance(m, str) or m.get("available", True)
            ]

        instance_id = explicit.get("providerInstanceId")
        model = explicit.get("model")
        if not instance_id:
            instance_id = next(
                (key for key, entry in live.items() if model and model in ids(entry)),
                None,
            )
        provider = live.get(instance_id)
        if self._gate(instance_id, provider, timestamp, reserved):
            return []
        offered = ids(provider)
        if model is None:
            config = next(
                (
                    c
                    for c in self.config.providers.values()
                    if c.instance_id == instance_id
                ),
                None,
            )
            preferred = [m for m in (config.models if config else []) if m in offered]
            model = (preferred or offered or [None])[0]
        if not model or model not in offered:
            self.last_blockers.append(
                {
                    "provider": instance_id,
                    "reason": "explicit_model_not_in_host_catalog",
                }
            )
            return []
        self.last_reason = "ready"
        return [Target(instance_id, model, kind)]
