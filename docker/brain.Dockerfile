# The brain: habodek-brain and its code intelligence (Python 3.12).
#
# Built on the VPN host: the base image comes through the Artifactory Docker proxy
# and every Python package through its PyPI proxy (docker/build.sh passes both).
# ripgrep comes from PyPI too (the `ripgrep` wheel), so nothing needs apt.
#
# Usable on its own for standalone runs, e.g. the scenario scorecard:
#   docker run --rm -e HABODEK_LLM_BASE_URL=... -e HABODEK_LLM_TOKEN=... \
#       habodek-brain:latest scorecard --scenarios 1-9 --runs 3
ARG PYTHON_IMAGE=python:3.12-bookworm
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=""
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 \
    HABODEK_HOME=/var/lib/habodek
WORKDIR /opt/habodek/src
COPY pyproject.toml NOTICE README.md ./
COPY habodek habodek
COPY eval eval
COPY tests tests
COPY probe probe
COPY docker docker
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} . ripgrep==14.1.0 \
    && habodek-brain --version \
    && rg --version > /dev/null \
    && git --version > /dev/null \
    && python docker/licences.py --python > /opt/habodek/licences-python.tsv
RUN useradd --uid 10001 --create-home habodek \
    && mkdir -p /var/lib/habodek && chown habodek /var/lib/habodek
USER habodek
ENTRYPOINT ["habodek-brain"]
