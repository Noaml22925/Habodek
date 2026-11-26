# The control plane: habodek-server and the habodek CLI, on top of the brain image
# (the server runs habodek-brain as a subprocess per job).
#
# Go builds offline from agent/vendor (GOPROXY=off, GOTOOLCHAIN=local): the toolchain
# image must be Go >= 1.25.5, the version agent/go.mod declares. The e2e test binary
# is built too, so the egress test runs in the image that is deployed.
ARG GO_IMAGE=golang:1.25.5-bookworm
ARG DOCKER_CLI_IMAGE=docker:27.5-cli
ARG BRAIN_IMAGE=habodek-brain:latest

FROM ${GO_IMAGE} AS build
ENV GOFLAGS=-mod=vendor GOPROXY=off GOTOOLCHAIN=local CGO_ENABLED=1
WORKDIR /src/agent
COPY agent ./
RUN go build -trimpath -o /out/habodek-server ./cmd/server \
    && go build -trimpath -o /out/habodek ./cmd/cli \
    && go test -c -o /out/habodek-e2e.test ./internal/orchestrator

# The docker CLI, for the sandbox's sibling containers (the socket is mounted).
FROM ${DOCKER_CLI_IMAGE} AS dockercli

FROM ${BRAIN_IMAGE}
USER root
COPY --from=build /out/ /usr/local/bin/
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY agent /opt/habodek/src/agent
RUN python docker/licences.py --go agent/vendor > /opt/habodek/licences-go.tsv \
    && chmod +x docker/egress/run.sh
ENV HABODEK_BRAIN_CMD=habodek-brain \
    HABODEK_WORK_DIR=/var/lib/habodek/jobs \
    HABODEK_TRACE_DIR=/var/lib/habodek/traces \
    HABODEK_MEMORY_DIR=/var/lib/habodek/memory \
    HABODEK_SANDBOX_CACHE=/var/lib/habodek/sandbox \
    HABODEK_SANDBOX_WORK=/var/lib/habodek/sandbox-runs \
    GIT_REPOS_DIR=/var/lib/habodek/repos \
    SQLITE_PATH=/var/lib/habodek/habodek.db
USER habodek
EXPOSE 8080
ENTRYPOINT ["habodek-server"]
