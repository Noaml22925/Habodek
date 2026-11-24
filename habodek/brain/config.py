"""Runtime configuration from the environment.

Everything that differs between a dev box, a scenario run and the VPN is here, read
once. Model behaviour lives in llm/models.yaml, not here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _float(env: dict[str, str], key: str, default: float) -> float:
    value = env.get(key)
    return float(value) if value else default


@dataclass(frozen=True)
class Config:
    llm_base_url: str = ""
    llm_token: str = ""
    models_yaml: Path | None = None
    rpm: float = 25.0  # per model (the gateway limits per model); the proxy is the real fence
    rpm_burst: float = 2.0
    request_timeout_s: float = 900.0
    evidence_pack_tokens: int = 80_000
    pip_index_url: str = ""  # Artifactory, for pydeps
    pip_find_links: tuple[str, ...] = ()  # local wheel dirs (offline runs, tests)
    internal_packages: tuple[str, ...] = ()  # names or fnmatch patterns
    deps_cache: Path = field(default_factory=lambda: Path("deps-cache"))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ) if env is None else env
        internal = tuple(p.strip() for p in env.get("HABODEK_INTERNAL_PACKAGES", "").split(",")
                         if p.strip())
        return cls(
            llm_base_url=env.get("HABODEK_LLM_BASE_URL", ""),
            llm_token=env.get("HABODEK_LLM_TOKEN", ""),
            models_yaml=Path(env["HABODEK_MODELS_YAML"]) if env.get("HABODEK_MODELS_YAML")
            else None,
            rpm=_float(env, "HABODEK_RPM", 25.0),
            rpm_burst=_float(env, "HABODEK_RPM_BURST", 2.0),
            request_timeout_s=_float(env, "HABODEK_LLM_TIMEOUT", 900.0),
            evidence_pack_tokens=int(_float(env, "HABODEK_EVIDENCE_TOKENS", 80_000)),
            pip_index_url=env.get("HABODEK_PIP_INDEX_URL", ""),
            pip_find_links=tuple(p for p in env.get("HABODEK_PIP_FIND_LINKS", "").split(",")
                                 if p.strip()),
            internal_packages=internal,
            deps_cache=Path(env.get("HABODEK_DEPS_CACHE", "deps-cache")),
        )
