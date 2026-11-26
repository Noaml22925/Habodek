#!/bin/sh
# The egress test (handoff M12). Inside the control image:
#   docker run --rm --network none --entrypoint /opt/habodek/src/docker/egress/run.sh habodek-control:TAG
# On a host with unprivileged user namespaces, from the repository root:
#   docker/egress/run.sh --netns
set -e
here=$(cd "$(dirname "$0")" && pwd)
exec python3 "$here/egress.py" "$@"
