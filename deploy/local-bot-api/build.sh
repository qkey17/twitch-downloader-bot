#!/usr/bin/env bash
# Build/verify only, on a disposable Ubuntu 24.04 x86_64 runner. Never on Oracle.
set -euo pipefail
umask 022

readonly UPSTREAM_COMMIT=e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1
readonly TDLIB_COMMIT=bc9c263e2bfee06aaab41e82db51a103376030bc
readonly API_VERSION=10.3
PROJECT_DIR=$(git rev-parse --show-toplevel)
readonly PROJECT_DIR
[[ $# == 1 && "$1" == /* ]] || { echo 'Supply a new absolute CI build directory.' >&2; exit 1; }
readonly WORK_DIR="$1"
[[ ! -e "$WORK_DIR" ]] || { echo 'Build directory already exists; nothing was removed.' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'Expected x86_64.' >&2; exit 1; }
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo 'Expected Ubuntu 24.04.' >&2; exit 1; }
unset TELEGRAM_API_ID TELEGRAM_API_HASH
mkdir -p "$WORK_DIR/source" "$WORK_DIR/bundle"
git -C "$WORK_DIR/source" init -q
git -C "$WORK_DIR/source" remote add origin https://github.com/tdlib/telegram-bot-api.git
git -C "$WORK_DIR/source" fetch --depth=1 origin "$UPSTREAM_COMMIT"
git -C "$WORK_DIR/source" checkout --detach FETCH_HEAD
git -C "$WORK_DIR/source" submodule update --init --recursive --depth=1
[[ $(git -C "$WORK_DIR/source" rev-parse HEAD) == "$UPSTREAM_COMMIT" ]]
[[ $(git -C "$WORK_DIR/source/td" rev-parse HEAD) == "$TDLIB_COMMIT" ]]
git -C "$WORK_DIR/source" submodule status --recursive > "$WORK_DIR/bundle/upstream-revisions.txt"
if grep -Eq '^[+U-]' "$WORK_DIR/bundle/upstream-revisions.txt"; then
    echo 'Submodules do not match the pinned gitlinks.' >&2
    exit 1
fi
cmake -S "$WORK_DIR/source" -B "$WORK_DIR/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
cmake --build "$WORK_DIR/build" --target telegram-bot-api --parallel 2
install -m 0755 "$WORK_DIR/build/telegram-bot-api" "$WORK_DIR/bundle/telegram-bot-api"
cd "$WORK_DIR/bundle"
file telegram-bot-api | tee architecture.txt
readelf -h telegram-bot-api >> architecture.txt
grep -q 'ELF 64-bit.*x86-64' architecture.txt
ldd ./telegram-bot-api | tee runtime-dependencies.txt
! grep -q 'not found' runtime-dependencies.txt
timeout 15 ./telegram-bot-api --help > help.txt 2>&1
timeout 15 ./telegram-bot-api --version > version.txt 2>&1
grep -Fq "$API_VERSION" version.txt
for option in --local --http-ip-address --http-port --dir --files-dir --temp-dir --verbosity --memory-verbosity; do
    grep -Fq -- "$option" help.txt
done
sha256sum telegram-bot-api > SHA256SUMS
sha256sum --check SHA256SUMS
{
    printf 'API_VERSION=%s\nUPSTREAM_COMMIT=%s\nTDLIB_COMMIT=%s\n' "$API_VERSION" "$UPSTREAM_COMMIT" "$TDLIB_COMMIT"
    printf 'TCD_COMMIT=%s\n' "$(git -C "$PROJECT_DIR" rev-parse HEAD)"
    printf 'BUILT_AT_UTC=%s\n' "$(date -u +%FT%TZ)"
    printf 'BUILD_TYPE=Release\nCOMPILER=clang++\nPARALLEL=2\n'
    uname -a
    cat /etc/os-release
    clang++ --version
    cmake --version
    systemd-analyze --version
    dpkg-query -W libc6 libstdc++6 libssl3t64 zlib1g
} > build-info.txt
echo 'Binary prepared. No bot credentials, login, installation on Oracle or deployment.'
