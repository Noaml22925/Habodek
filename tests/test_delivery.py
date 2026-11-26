"""The delivery files stay true to the code (M12).

Images cannot be built in development (no Docker daemon), so these checks catch what
would otherwise surface only on the VPN host: a Dockerfile copying a path that no
longer exists, or a configuration template naming a variable nothing reads.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"


def _read_by_code() -> str:
    parts = [(ROOT / "agent/internal/config/config.go").read_text()]
    parts += [p.read_text() for p in (ROOT / "habodek").rglob("*.py")]
    parts += [(ROOT / "agent/internal/core/brain/runner.go").read_text()]
    return "\n".join(parts)


def test_dockerfiles_copy_paths_that_exist():
    for dockerfile in DOCKER.glob("*.Dockerfile"):
        context = DOCKER if dockerfile.name == "runner.Dockerfile" else ROOT
        for line in dockerfile.read_text().splitlines():
            m = re.match(r"COPY\s+(?!--from)(.+)\s+\S+$", line.strip())
            if not m:
                continue
            for src in m.group(1).split():
                assert (context / src).exists(), f"{dockerfile.name}: COPY {src} is missing"


def test_every_template_variable_is_read_by_the_code():
    code = _read_by_code()
    for template in ("habodek.env.example",):
        for line in (DOCKER / template).read_text().splitlines():
            m = re.match(r"([A-Z][A-Z0-9_]+)=", line)
            if m:
                assert f'"{m.group(1)}' in code, f"{template}: nothing reads {m.group(1)}"
    compose = (DOCKER / "compose.yml").read_text()
    block = compose.split("habodek:", 1)[1].split("ports:", 1)[0]
    for key in re.findall(r"^\s{6}([A-Z][A-Z0-9_]+):", block, re.MULTILINE):
        assert f'"{key}' in code, f"compose.yml: nothing reads {key}"


def test_agent_env_example_is_v2():
    text = (ROOT / "agent/.env.example").read_text()
    code = _read_by_code()
    for key in re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE):
        assert f'"{key}' in code, f"agent/.env.example: nothing reads {key}"


def test_build_script_and_egress_entry_point_agree_with_the_images():
    build = (DOCKER / "build.sh").read_text()
    for name in ("brain", "control", "runner"):
        assert f"docker/{name}.Dockerfile" in build
    control = (DOCKER / "control.Dockerfile").read_text()
    assert "/opt/habodek/src/docker/egress/run.sh" in build and "COPY docker docker" in \
        (DOCKER / "brain.Dockerfile").read_text()
    assert "habodek-e2e.test" in control and "habodek-e2e.test" in \
        (DOCKER / "egress" / "egress.py").read_text()
    assert "GOPROXY=off" in control and "-mod=vendor" in control
