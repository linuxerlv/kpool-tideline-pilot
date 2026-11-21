#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[[ $# -eq 2 ]] || fail 'Pass the full digest-pinned image reference and the 40-character source SHA.'
reference=$1
source_sha=$2
platform=linux/amd64
source_url=https://github.com/linuxerlv/kpool-tideline-pilot

[[ "$reference" =~ ^ghcr\.io/linuxerlv/kpool-tideline-pilot@sha256:[0-9a-f]{64}$ ]] ||
    fail 'Expected a digest-pinned reference for ghcr.io/linuxerlv/kpool-tideline-pilot.'
[[ "$source_sha" =~ ^[0-9a-f]{40}$ ]] || fail 'Expected a full lowercase source SHA.'

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ "$(git rev-parse --verify HEAD)" == "$source_sha" ]] || fail 'Checkout HEAD differs from the source SHA.'

docker pull --platform "$platform" "$reference" >/dev/null
repo_digests=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$reference")
grep -Fxq -- "$reference" <<< "$repo_digests" || fail 'The pulled image does not report the requested repository digest.'
[[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$reference")" == "$platform" ]] ||
    fail 'The pulled image platform differs from linux/amd64.'
revision_label=$(docker image inspect --format '{{json (index .Config.Labels "org.opencontainers.image.revision")}}' "$reference")
[[ "$revision_label" == "\"$source_sha\"" ]] || fail 'The OCI revision label differs from the source SHA.'
source_label=$(docker image inspect --format '{{json (index .Config.Labels "org.opencontainers.image.source")}}' "$reference")
[[ "$source_label" == "\"$source_url\"" ]] || fail 'The OCI source label differs from the pilot repository URL.'

scratch=$(mktemp -d)
container=''
cleanup() {
    local status=$?
    if [[ -n "$container" ]]; then
        docker rm --force "$container" >/dev/null || status=1
    fi
    rm -f -- "$scratch/asset" "$scratch/committed" || status=1
    rmdir -- "$scratch" || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

container=$(docker create --pull=never --platform "$platform" --publish 127.0.0.1::80 "$reference")
docker start "$container" >/dev/null
address=$(docker port "$container" 80/tcp)
[[ "$address" =~ ^127\.0\.0\.1:[0-9]+$ ]] || fail 'Expected a single loopback-only port binding.'
base="http://$address"

curl_options=(-q --noproxy '*' --fail --silent --show-error --max-time 5)
health_status=$(curl "${curl_options[@]}" --retry 10 --retry-connrefused --retry-delay 1 --retry-max-time 15 \
    --output /dev/null --write-out '%{response_code}' "$base/healthz")
[[ "$health_status" == 200 ]] || fail 'Expected HTTP 200 from /healthz.'

file_proof=()
for file in index.html styles.css game.mjs game-engine.mjs; do
    path="/$file"
    case "$file" in
        index.html) path=/; expected_type=text/html ;;
        *.mjs) expected_type=text/javascript ;;
        *.css) expected_type=text/css ;;
    esac
    response=$(curl "${curl_options[@]}" --output "$scratch/asset" --write-out '%{response_code} %{content_type}' "$base$path")
    [[ "${response%% *}" == 200 ]] || fail "Expected HTTP 200 from $path."
    content_type=${response#* }
    [[ "${content_type%%;*}" == "$expected_type" ]] || fail "Expected $expected_type from $path."

    git cat-file blob "$source_sha:$file" > "$scratch/committed"
    cmp -s -- "$file" "$scratch/committed" || fail "Checkout $file differs from the source commit."
    cmp -s -- "$scratch/asset" "$scratch/committed" || fail "Served $path differs from the source commit."
    checksum=$(sha256sum < "$scratch/asset")
    printf -v entry '"%s":"%s"' "$file" "${checksum%% *}"
    file_proof+=("$entry")
done

files_json=$(IFS=,; printf '%s' "${file_proof[*]}")
printf 'KPOOL_GHCR_VERIFY_OK {"reference":"%s","source_sha":"%s","files":{%s},"health_status":%s,"platform":"%s"}\n' \
    "$reference" "$source_sha" "$files_json" "$health_status" "$platform"
