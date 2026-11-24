# The sandbox runner image (M9): what repository code runs in.
#
#   docker build -f docker/runner.Dockerfile -t habodek-runner:latest \
#       --build-arg PIP_INDEX_URL=https://artifactory.example/api/pypi/pypi/simple docker/
#
# The control plane starts it twice per revision (internal/pkg/sandbox): env-prep
# builds the repository's venv on the internal network (pip -> Artifactory), and each
# run uses it with --network none, a read-only root, an unprivileged user and limits.
# The venv is created with --system-site-packages, so the test tools installed here
# are visible to every repository. Not built in development (no Docker daemon there);
# M12 wires it into the delivery bundle.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=""
RUN pip install --no-cache-dir --disable-pip-version-check \
        ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
        pytest==8.3.5 \
    && rm -rf /root/.cache
USER 65534:65534
WORKDIR /work
