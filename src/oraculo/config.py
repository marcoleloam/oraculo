from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutingTraits(StrictModel):
    capability: Literal[
        "general", "coding", "reasoning", "research", "verification"
    ] = "general"
    latency: Literal["standard", "fast", "deliberate"] = "standard"


class ProviderConfig(StrictModel):
    instance_id: str
    auth_kind: Literal["existing_subscription"] = "existing_subscription"
    billing_verified: bool = False
    models: list[str] = Field(default_factory=list)
    routing_traits: dict[str, RoutingTraits] = Field(default_factory=dict)
    # None means no provider-specific ceiling: the service ceiling applies.
    max_executors: int | None = Field(default=None, ge=1, strict=True)


class MemoryConfig(StrictModel):
    max_batch_pages: int = Field(default=20, ge=1, le=20)
    max_proposal_bytes: int = Field(default=2097152, ge=1, le=2097152)
    review_retention_days: int = Field(default=30, ge=1)
    canonical_gc: Literal[False] = False


class DecisionsTariff(StrictModel):
    version: str = Field(min_length=1, max_length=100)
    authorization_reference: str = Field(pattern=r"^ref:[a-zA-Z0-9_.-]{1,100}$")
    bound_reference: str = Field(pattern=r"^ref:[a-zA-Z0-9_.-]{1,100}$")
    # Authorized upper rates in nano-USD/token, including caching premiums.
    input_nanousd: int = Field(ge=1, strict=True)
    multiplier_milli: int = Field(ge=1000, strict=True)
    framing_tokens: int = Field(ge=1024, strict=True)
    # Decisions currently has no output cap. Only the explicit zero-output
    # billing contract can supply a finite bound for this adapter.
    output_nanousd: Literal[0] = 0


class DecisionsConfig(StrictModel):
    # API spend is independent of subscription billing; defaults cannot send.
    enabled: bool = Field(default=False, strict=True)
    mode: Literal["off", "shadow", "assisted", "active"] = "off"
    model: Literal["gpt-6-luna"] = "gpt-6-luna"
    credential_reference: str | None = Field(
        default=None, pattern=r"^ref:[a-zA-Z0-9_.-]{1,100}$"
    )
    daily_budget_microusd: int = Field(default=0, ge=0, le=1000000, strict=True)
    run_budget_microusd: int = Field(default=0, ge=0, le=1000000, strict=True)
    # Logical task, across contexts/retries/restarts. run_budget remains a
    # legacy shadow ceiling; active/assisted use task_budget only.
    task_budget_microusd: int = Field(default=100000, ge=0, le=100000, strict=True)
    max_calls: int = Field(default=80, ge=0, le=80, strict=True)
    max_concurrent: int = Field(default=2, ge=1, le=2, strict=True)
    deadline_seconds: float = Field(default=3, gt=0, le=3)
    tariff: DecisionsTariff | None = None


class RolloutTarget(StrictModel):
    # Pinned by service configuration, never by an executor request.
    verifier_argv: list[str] = Field(default_factory=list, max_length=100)
    cwd: Path | None = None
    runtime_root: Path | None = None

    @model_validator(mode="after")
    def destination(self):
        if self.runtime_root is not None:
            if self.verifier_argv or self.cwd is not None:
                raise ValueError("runtime destination cannot also execute argv")
        elif not self.verifier_argv or self.cwd is None:
            raise ValueError("verifier argv and cwd or runtime_root required")
        return self


class Config(StrictModel):
    _loaded_sha256: str | None = PrivateAttr(default=None)
    schema_version: Literal[1] = 1
    projects_root: Path = Path.home() / "projects/work"
    # None preserves root-based discovery; [] authorizes no project.
    allowed_projects: list[Path] | None = None
    vault_root: Path = Path.home() / "Obsidian/Oraculo"
    state_dir: Path = Path.home() / "Library/Application Support/Oraculo"
    t3_mcp_url: str = "https://t3.example.invalid/mcp"
    max_executors: int = Field(default=1, ge=1)
    poll_active_seconds: int = Field(default=15, ge=1)
    poll_idle_seconds: int = Field(default=60, ge=1)
    proactive_interval_seconds: int = Field(default=1800, ge=1)
    max_dispatch_attempts: int = Field(default=3, ge=1, le=3)
    max_correction_rounds: int = Field(default=2, ge=0, le=2)
    quota_backoff_seconds: int = Field(default=1800, ge=1)
    additional_spend_allowed: Literal[False] = False
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "codex": ProviderConfig(instance_id="codex"),
            "claude": ProviderConfig(instance_id="claudeAgent"),
            "grok": ProviderConfig(instance_id="grok"),
        }
    )
    profiles: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "implementation": ["codex", "claude", "grok"],
            "tests": ["codex", "claude", "grok"],
            "architecture": ["claude", "codex", "grok"],
            "research": ["grok", "claude", "codex"],
            "discovery": ["grok", "claude", "codex"],
            "memory": ["codex", "claude", "grok"],
        }
    )
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    decisions: DecisionsConfig = Field(default_factory=DecisionsConfig)
    rollout_targets: dict[str, RolloutTarget] = Field(default_factory=dict)

    @field_validator("projects_root", "vault_root", "state_dir", mode="before")
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("allowed_projects", mode="before")
    @classmethod
    def expand_allowed_projects(
        cls, value: list[str | Path] | None
    ) -> list[Path] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("allowed_projects must be a list of project paths")
        return [Path(path).expanduser().resolve() for path in value]

    @field_validator("t3_mcp_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.username or url.password or url.fragment:
            raise ValueError("MCP URL must not embed credentials or fragments")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("MCP requires HTTPS or loopback HTTP")
        return value

    @model_validator(mode="after")
    def separated_paths(self) -> Self:
        for project in self.allowed_projects or []:
            if project == self.projects_root or not project.is_relative_to(
                self.projects_root
            ):
                raise ValueError(
                    "allowed_projects must be strictly inside projects_root"
                )
        for path in (self.vault_root, self.state_dir):
            if path == self.projects_root or path.is_relative_to(self.projects_root):
                raise ValueError("vault and operational state must be outside projects")
        if self.state_dir == self.vault_root or self.state_dir.is_relative_to(
            self.vault_root
        ):
            raise ValueError("operational state must be outside the vault")
        return self

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "control.sock"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    def project_in_scope(self, path: str | Path) -> bool:
        candidate = Path(path).expanduser().resolve()
        in_root = candidate != self.projects_root and candidate.is_relative_to(
            self.projects_root
        )
        if not in_root:
            return False
        # None keeps root discovery. A list is exact; dynamic policy only adds.
        if self.allowed_projects is None or candidate in self.allowed_projects:
            return True
        from oraculo.project_registration import dynamic_scope_contains

        return dynamic_scope_contains(self, candidate)

    @classmethod
    def load(cls, path: Path | str) -> Config:
        raw = Path(path).expanduser().read_bytes()
        config = cls.model_validate(tomllib.loads(raw.decode("utf-8")))
        config._loaded_sha256 = hashlib.sha256(raw).hexdigest()
        return config


def load_config(path: Path | str) -> Config:
    return Config.load(path)


def resources_root() -> Path:
    packaged = Path(__file__).resolve().parent / "assets"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2]
