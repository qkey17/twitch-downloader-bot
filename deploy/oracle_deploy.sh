#!/usr/bin/env bash
# Install a reviewed copy as root-owned /usr/local/libexec/tdb-deploy.
# Run updates as ubuntu, NEVER as root. Root is allowed only the ready probe.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export GIT_TERMINAL_PROMPT=0
umask 077

readonly PROJECT=/home/ubuntu/twitch-downloader-bot
readonly STATE_DIR=/home/ubuntu/.local/state/tdb-deploy
readonly HELPER=/usr/local/libexec/tdb-deploy
readonly UNIT=tdb.service
readonly STOP_TIMEOUT=600 START_TIMEOUT=180 STABILITY_SECONDS=10
stage=protocol
service_touched=0
target=''
previous=''
manifest=''

fail() { printf 'ERROR [%s]: %s\n' "$stage" "$1" >&2; exit 1; }
note() { printf '%s\n' "$1" >&2; }
property() { /usr/bin/systemctl show "$UNIT" --property="$1" --value; }
git_cmd() { git -c core.hooksPath=/dev/null "$@"; }

ready() {
    local invocation pid
    [[ $(property ActiveState) == active && $(property SubState) == running ]] || return 1
    invocation=$(property InvocationID)
    [[ "$invocation" =~ ^[0-9a-f]{32}$ ]] || return 1
    pid=$(property MainPID)
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    # Never return journal text to SSH/Actions, even when it contains secrets.
    /usr/bin/journalctl --quiet --no-pager --unit="$UNIT" \
        "_SYSTEMD_INVOCATION_ID=$invocation" "_PID=$pid" --output=cat \
        --grep='Application started$' --lines=1 2>/dev/null |
        grep -E '[|] INFO [|] telegram[.]ext[.]Application [|] Application started$' >/dev/null
}

finish() {
    local result=$?
    local head='' dirty='unknown'
    trap - EXIT HUP INT TERM
    set +e
    if [[ -n "$manifest" ]]; then rm -f -- "$manifest"; fi
    if (( result != 0 )); then
        note "Deployment/check failed at stage: $stage (exit $result)."
        if (( service_touched )); then
            head=$(git_cmd -C "$PROJECT" rev-parse HEAD 2>/dev/null)
            dirty=$(git_cmd -C "$PROJECT" status --porcelain --untracked-files=no 2>/dev/null) || dirty=unknown
            if [[ -n "$dirty" || ( "$head" != "$previous" && "$head" != "$target" ) ]]; then
                note 'Recovery start withheld: checkout is inconsistent. Manual repair is required.'
            elif sudo -n /usr/bin/systemctl --no-block start "$UNIT" >/dev/null 2>&1; then
                note 'Recovery start requested. This does NOT mean deployment succeeded.'
            else
                note 'Recovery start failed. An administrator must inspect the service.'
            fi
        fi
        # Fixed non-secret properties only; no status output or raw journal.
        /usr/bin/systemctl show "$UNIT" --property=ActiveState --property=SubState \
            --property=Result --property=ExecMainStatus --property=NRestarts >&2 2>/dev/null
        note 'Inspect journalctl on the VM through your administrative SSH session.'
    fi
    exit "$result"
}

preflight() {
    local remote exec_start dirty
    stage=preflight
    [[ $(id -un) == ubuntu ]] || fail 'Run this helper as ubuntu.'
    [[ -d "$PROJECT/.git" && -x "$PROJECT/.venv/bin/python" ]] || fail 'Checkout or .venv is missing.'
    cd "$PROJECT"
    [[ $(git_cmd rev-parse --show-toplevel) == "$PROJECT" ]] || fail 'Unexpected checkout root.'
    [[ $(git_cmd branch --show-current) == main ]] || fail 'The server checkout must be on main.'
    dirty=$(git_cmd status --porcelain --untracked-files=no 2>/dev/null) || fail 'Could not inspect tracked server files.'
    [[ -z "$dirty" ]] || fail 'Tracked server files are modified; refusing to overwrite them.'
    remote=$(git_cmd remote get-url origin)
    case "$remote" in
        https://github.com/qkey17/twitch-downloader-bot|https://github.com/qkey17/twitch-downloader-bot.git|git@github.com:qkey17/twitch-downloader-bot.git) ;;
        *) fail 'origin must point to the expected GitHub repository without embedded credentials.' ;;
    esac
    [[ $(property WorkingDirectory) == "$PROJECT" ]] || fail 'Unexpected service WorkingDirectory.'
    exec_start=$(property ExecStart)
    [[ "$exec_start" == *"$PROJECT/.venv/bin/python -m src.main"* ]] || fail 'Unexpected service ExecStart.'
    [[ $(property Type) == simple ]] || fail 'The helper expects a Type=simple service.'
    [[ $(property TimeoutStopUSec) == infinity && $(property KillSignal) == 15 && $(property KillMode) == control-group && $(property SendSIGKILL) == no ]] ||
        fail 'Configure graceful systemd shutdown as documented before deploying.'
    [[ $(property StandardOutput) == journal ]] || fail 'Service stdout must use the journal.'
    [[ $(property StandardError) == journal || $(property StandardError) == inherit ]] || fail 'Service stderr must use the journal.'
    [[ $(property ActiveState) == active && $(property SubState) == running ]] || fail 'The service is not running; investigate before deploying.'
    sudo -n -l -- /usr/bin/systemctl --no-block stop "$UNIT" >/dev/null 2>&1 || fail 'Missing permission to stop the service.'
    sudo -n -l -- /usr/bin/systemctl --no-block start "$UNIT" >/dev/null 2>&1 || fail 'Missing permission to start the service.'
    sudo -n -l -- "$HELPER" ready >/dev/null 2>&1 || fail 'Missing permission to run the private readiness probe.'
    timeout 60 "$PROJECT/.venv/bin/python" -m pip check >/dev/null 2>&1 || fail 'The existing virtual environment is inconsistent.'
}

reject_protected_paths() {
    local path
    while IFS= read -r -d '' path; do
        case "$path" in
            .env.example|src/viewer_template.html) ;;
            .env|.env.*|*/.env|*/.env.*|.venv|.venv/*|venv|venv/*|_out|_out/*|_work|_work/*|*.db|*.db-*|*.sqlite|*.sqlite-*|*.sqlite3|*.sqlite3-*|*.html|*.tmp|*.pem|*.key|*.secret|*.secrets|*/id_ed25519|*/id_rsa)
                fail 'The selected update touches a protected runtime/secret path.' ;;
        esac
    done < "$manifest"
}

check_target() {
    local path parent
    stage=fetch
    timeout 120 git -c core.hooksPath=/dev/null fetch --no-tags origin refs/heads/main:refs/remotes/origin/main >/dev/null 2>&1 || fail 'Could not fetch main.'
    git_cmd cat-file -e "$target^{commit}" 2>/dev/null || fail 'The selected commit is not available.'
    git_cmd merge-base --is-ancestor "$target" refs/remotes/origin/main || fail 'The selected commit is not on origin/main.'
    previous=$(git_cmd rev-parse HEAD)
    git_cmd merge-base --is-ancestor "$previous" "$target" || fail 'Refusing a downgrade or divergent update.'
    # Dependencies and schema changes need a separately reviewed maintenance plan.
    git_cmd diff --quiet "$previous" "$target" -- requirements.txt src/database || fail 'Dependency/database changes require manual maintenance and backups.'
    # Check command status before reading NUL-delimited paths; process substitution
    # would silently accept an empty list when Git fails.
    manifest=$(mktemp "$STATE_DIR/paths.XXXXXXXX") || fail 'Could not create a private path manifest.'
    git_cmd ls-tree -r --name-only -z "$target" > "$manifest" 2>/dev/null || fail 'Could not inspect the selected tree.'
    reject_protected_paths
    git_cmd diff --no-renames --name-only -z "$previous" "$target" > "$manifest" 2>/dev/null || fail 'Could not inspect changed paths.'
    reject_protected_paths  # Includes deleted paths, absent from the target tree.
    git_cmd diff --no-renames --name-only --diff-filter=A -z "$previous" "$target" > "$manifest" 2>/dev/null || fail 'Could not inspect newly tracked paths.'
    while IFS= read -r -d '' path; do
        [[ ! -e "$PROJECT/$path" && ! -L "$PROJECT/$path" ]] || fail 'A newly tracked path would collide with an existing local file.'
        parent=${path%/*}
        while [[ "$parent" != "$path" ]]; do
            [[ ! -L "$PROJECT/$parent" ]] || fail 'A newly tracked path has a local symlink ancestor.'
            [[ ! -e "$PROJECT/$parent" || -d "$PROJECT/$parent" ]] || fail 'A newly tracked path has a local file ancestor.'
            path=$parent
            parent=${path%/*}
        done
    done < "$manifest"
    rm -f -- "$manifest"
    manifest=''
    note "Selected commit: $target; previous commit: $previous."
}

deploy() {
    local previous_invocation deadline state invocation pid restarts dirty
    check_target
    previous_invocation=$(property InvocationID)
    stage=stop
    service_touched=1  # Also recover if the stop request itself fails.
    sudo -n /usr/bin/systemctl --no-block stop "$UNIT" >/dev/null 2>&1 || fail 'Could not request a graceful stop.'
    deadline=$((SECONDS + STOP_TIMEOUT))
    while :; do
        state=$(property ActiveState)
        if [[ "$state" == inactive && $(property MainPID) == 0 ]]; then break; fi
        [[ "$state" != failed ]] || fail 'The service failed while stopping; code was not updated.'
        (( SECONDS < deadline )) || fail 'Graceful stop timed out; code was not updated. A restart will be queued.'
        sleep 2
    done
    stage=update
    timeout 60 git -c core.hooksPath=/dev/null merge --ff-only --no-edit --no-overwrite-ignore "$target" >/dev/null 2>&1 || fail 'Fast-forward update failed; no reset/clean will be attempted.'
    dirty=$(git_cmd status --porcelain --untracked-files=no 2>/dev/null) || fail 'Could not inspect the updated checkout.'
    [[ $(git_cmd rev-parse HEAD) == "$target" && -z "$dirty" ]] || fail 'Updated checkout is not clean at the selected commit.'
    stage=imports
    timeout 60 "$PROJECT/.venv/bin/python" -m compileall -q src >/dev/null 2>&1 || fail 'New source compilation failed.'
    timeout 60 "$PROJECT/.venv/bin/python" -c 'import src.main' >/dev/null 2>&1 || fail 'New source imports failed.'
    stage=start
    sudo -n /usr/bin/systemctl --no-block start "$UNIT" >/dev/null 2>&1 || fail 'Could not request service startup.'
    deadline=$((SECONDS + START_TIMEOUT))
    while :; do
        invocation=$(property InvocationID)
        if [[ $(property ActiveState) == active && $(property SubState) == running && "$invocation" != "$previous_invocation" && "$invocation" =~ ^[0-9a-f]{32}$ ]] &&
            sudo -n "$HELPER" ready >/dev/null 2>&1; then break; fi
        (( SECONDS < deadline )) || fail 'New Application startup was not confirmed in the journal.'
        sleep 2
    done
    stage=stability
    pid=$(property MainPID)
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || fail 'No running service process.'
    restarts=$(property NRestarts)
    sleep "$STABILITY_SECONDS"
    [[ $(property ActiveState) == active && $(property SubState) == running && $(property InvocationID) == "$invocation" && $(property MainPID) == "$pid" && $(property NRestarts) == "$restarts" ]] || fail 'The service stopped or restarted during verification.'
    sudo -n "$HELPER" ready >/dev/null 2>&1 || fail 'Application startup verification failed.'
    [[ $(git_cmd rev-parse HEAD) == "$target" ]] || fail 'HEAD changed during verification.'
    service_touched=0
    printf 'DEPLOYED %s\n' "$target"
}

main() {
    # Only this fixed, argument-free journal probe is granted root privileges.
    if (( EUID == 0 )); then
        [[ $# == 1 && $1 == ready ]] || { note 'Root may only run the ready probe.'; return 1; }
        ready
        return
    fi
    [[ $# == 0 ]] || fail 'Use the restricted SSH command protocol.'
    local mode
    if [[ ${SSH_ORIGINAL_COMMAND:-} == check ]]; then
        mode=check
    elif [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
        mode=deploy
        target=${BASH_REMATCH[1]}
    else
        fail 'Allowed commands: check or deploy followed by a full lowercase SHA.'
    fi
    trap finish EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
    [[ ! -L "$STATE_DIR" ]] || fail 'The state directory must not be a symlink.'
    mkdir -p "$STATE_DIR"
    [[ $(cd -- "$STATE_DIR" && pwd -P) == "$STATE_DIR" ]] || fail 'The state directory must use its physical path.'
    chmod 700 "$STATE_DIR"
    [[ ! -L "$STATE_DIR/deploy.lock" ]] || fail 'The lock must not be a symlink.'
    exec 9>>"$STATE_DIR/deploy.lock"
    flock -n 9 || fail 'Another server deployment/check holds the lock.'
    preflight
    if [[ "$mode" == check ]]; then printf 'CHECK_OK protocol=1\n'; else deploy; fi
}

main "$@"
