"""Model profiles: the per-model rules from `models.yaml`, typed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODELS_YAML = Path(__file__).with_name("models.yaml")

# Environment overrides for gateway aliases: HABODEK_MODEL_<KEY>, where KEY is the
# profile name's first dotted/dashed word, upper-cased (qwen3.8-flash-next -> QWEN).
_ENV_PREFIX = "HABODEK_MODEL_"


@dataclass(frozen=True)
class Effort:
    param: str
    allowed: tuple[str, ...]
    default: str


@dataclass(frozen=True)
class Profile:
    name: str
    gateway_model: str
    fallback: str | None
    reasoning_echo: str  # every_assistant_turn | none
    reasoning_field: str
    effort: Effort | None
    never_send: tuple[str, ...]
    system_messages: str  # first_only | any
    sampling: dict[str, Any]
    context_window: int
    max_output_tokens: int
    thinking: str = "default"
    chat_template_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Common:
    tool_call_turn_content: str = ""
    validate_tool_args_before_history: bool = True
    degenerate_retries: int = 2
    degenerate_then: str = "other_model"


@dataclass(frozen=True)
class Profiles:
    profiles: dict[str, Profile]
    common: Common
    roles: dict[str, str]

    def get(self, name: str) -> Profile:
        if name in self.profiles:
            return self.profiles[name]
        for profile in self.profiles.values():  # accept the gateway alias too
            if profile.gateway_model == name:
                return profile
        raise KeyError(f"unknown model {name!r}; known: {sorted(self.profiles)}")

    def for_role(self, role: str) -> Profile:
        try:
            return self.get(self.roles[role])
        except KeyError:
            raise KeyError(f"no model configured for role {role!r}") from None

    def other(self, name: str) -> Profile | None:
        fallback = self.get(name).fallback
        return self.get(fallback) if fallback else None


def _env_key(name: str) -> str:
    head = name.replace(".", "-").split("-")[0]
    return _ENV_PREFIX + "".join(ch for ch in head if ch.isalpha()).upper()


def load_profiles(path: Path | str | None = None, env: dict[str, str] | None = None) -> Profiles:
    env = dict(os.environ) if env is None else env
    raw = yaml.safe_load(Path(path or DEFAULT_MODELS_YAML).read_text(encoding="utf-8"))
    profiles: dict[str, Profile] = {}
    for name, spec in (raw.get("profiles") or {}).items():
        effort = spec.get("effort")
        profiles[name] = Profile(
            name=name,
            gateway_model=env.get(_env_key(name)) or spec.get("gateway_model", name),
            fallback=spec.get("fallback"),
            reasoning_echo=spec.get("reasoning_echo", "every_assistant_turn"),
            reasoning_field=spec.get("reasoning_field", "reasoning_content"),
            effort=Effort(effort["param"], tuple(effort["allowed"]), effort["default"])
            if effort
            else None,
            never_send=tuple(spec.get("never_send") or ()),
            system_messages=spec.get("system_messages", "first_only"),
            sampling=dict(spec.get("sampling") or {}),
            context_window=int(spec.get("context_window", 131072)),
            max_output_tokens=int(spec.get("max_output_tokens", 32768)),
            thinking=spec.get("thinking", "default"),
            chat_template_kwargs=dict(spec.get("chat_template_kwargs") or {}),
        )
    common_raw = raw.get("common") or {}
    degenerate = common_raw.get("degenerate") or {}
    common = Common(
        tool_call_turn_content=common_raw.get("tool_call_turn_content", ""),
        validate_tool_args_before_history=common_raw.get(
            "validate_tool_args_before_history", True
        ),
        degenerate_retries=int(degenerate.get("retries", 2)),
        degenerate_then=degenerate.get("then", "other_model"),
    )
    roles = dict(raw.get("roles") or {})
    for role, model in roles.items():
        if model not in profiles:
            raise ValueError(f"role {role!r} names unknown model {model!r}")
    for profile in profiles.values():
        if profile.fallback and profile.fallback not in profiles:
            raise ValueError(f"{profile.name}: fallback {profile.fallback!r} is not a profile")
    return Profiles(profiles=profiles, common=common, roles=roles)
