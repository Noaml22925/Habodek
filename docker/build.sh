#!/usr/bin/env bash
# Build Habodek's images on the VPN host, check them, and run the egress test.
#
#   docker/build.sh --registry artifactory.example/docker-remote \
#                   --pip-index https://artifactory.example/api/pypi/pypi/simple [--tag T]
#                   [--skip-egress]
#
# Base images come through the Artifactory Docker proxy (--registry) and Python
# packages through its PyPI proxy (--pip-index); Go builds from the vendor tree with
# no network. Prints at most 20 lines; every step's log, the licence report and the
# image ids go to build-logs/<timestamp>/ (printed last).
set -uo pipefail

registry="" pip_index="" tag="latest" egress=1
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) registry="${2%/}"; shift 2 ;;
    --pip-index) pip_index="$2"; shift 2 ;;
    --tag) tag="$2"; shift 2 ;;
    --skip-egress) egress=0; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
if [ -z "$registry" ] || [ -z "$pip_index" ]; then
  echo "usage: docker/build.sh --registry <docker proxy> --pip-index <pypi simple url> [--tag T]"
  exit 2
fi
cd "$(dirname "$0")/.."
logs="build-logs/$(date +%Y-%m-%dT%H-%M-%S)"
mkdir -p "$logs"
started=$(date +%s)

step() {  # step NAME COMMAND...: run it, keep its log, stop on failure
  local name="$1"; shift
  if "$@" > "$logs/$name.log" 2>&1; then
    echo "ok    $name"
  else
    echo "FAIL  $name — last lines of $logs/$name.log:"
    tail -n 8 "$logs/$name.log" | cut -c1-150
    echo "details: $logs"
    exit 1
  fi
}

echo "habodek build $tag  ($(git rev-parse --short HEAD 2>/dev/null || echo 'no git'))"
step brain docker build -f docker/brain.Dockerfile -t "habodek-brain:$tag" \
  --build-arg PYTHON_IMAGE="$registry/python:3.12-bookworm" \
  --build-arg PIP_INDEX_URL="$pip_index" .
step control docker build -f docker/control.Dockerfile -t "habodek-control:$tag" \
  --build-arg GO_IMAGE="$registry/golang:1.25.5-bookworm" \
  --build-arg DOCKER_CLI_IMAGE="$registry/docker:27.5-cli" \
  --build-arg BRAIN_IMAGE="habodek-brain:$tag" .
step runner docker build -f docker/runner.Dockerfile -t "habodek-runner:$tag" \
  --build-arg PYTHON_IMAGE="$registry/python:3.12-slim" \
  --build-arg PIP_INDEX_URL="$pip_index" docker/
step runner-check docker run --rm --network none "habodek-runner:$tag" python -m pytest --version
step server-check docker run --rm --entrypoint habodek "habodek-control:$tag" --help

docker run --rm --entrypoint cat "habodek-control:$tag" /opt/habodek/licences-python.tsv \
  /opt/habodek/licences-go.tsv > "$logs/licences.tsv" 2> "$logs/licences.log"
total=$(wc -l < "$logs/licences.tsv")
review=$(grep -c $'\treview$' "$logs/licences.tsv" || true)
echo "licences: $total packages, $review to review (licences.tsv)"
grep $'\treview$' "$logs/licences.tsv" | head -n 3 | cut -f1-3 | sed 's/^/  review: /'

for image in brain control runner; do
  docker image inspect --format "habodek-$image:$tag {{.Id}} {{.Size}}" "habodek-$image:$tag"
done > "$logs/images.txt"

if [ "$egress" = 1 ]; then
  docker run --rm --network none --entrypoint /opt/habodek/src/docker/egress/run.sh \
    "habodek-control:$tag" > "$logs/egress.log" 2>&1
  code=$?
  head -n 8 "$logs/egress.log" | cut -c1-150
  [ "$code" = 0 ] || { echo "FAIL  egress — see $logs/egress.log"; echo "details: $logs"; exit 1; }
else
  echo "egress skipped (--skip-egress)"
fi
echo "built in $(( ($(date +%s) - started) / 60 )) min; images: habodek-{brain,control,runner}:$tag"
echo "details: $logs"
