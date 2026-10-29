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
readonly RECOVERY_STATE=/home/ubuntu/tdb-daily-state
readonly RECOVERY_CONFIG=/home/ubuntu/tdb-admin/daily-backup.json
readonly RECOVERY_SOCKET=/run/tdb-maintenance/control.sock
readonly STOP_TIMEOUT=600 START_TIMEOUT=180 STABILITY_SECONDS=10
stage=protocol
service_touched=0
target=''
previous=''
manifest=''
emergency=0
expected=''

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
    if (( emergency )); then
        recovery_preflight
    else
        [[ $(property ActiveState) == active && $(property SubState) == running ]] || fail 'The service is not running; investigate before deploying.'
    fi
    sudo -n -l -- /usr/bin/systemctl --no-block stop "$UNIT" >/dev/null 2>&1 || fail 'Missing permission to stop the service.'
    sudo -n -l -- /usr/bin/systemctl --no-block start "$UNIT" >/dev/null 2>&1 || fail 'Missing permission to start the service.'
    sudo -n -l -- "$HELPER" ready >/dev/null 2>&1 || fail 'Missing permission to run the private readiness probe.'
    timeout 60 "$PROJECT/.venv/bin/python" -m pip check >/dev/null 2>&1 || fail 'The existing virtual environment is inconsistent.'
}

stopped_unit() {
    # A single property snapshot; absent/unknown fields are never permission.
    local unit=$1 optional=${2:-no} data key value count=0
    local load='' active='' sub='' pid='' control='' tasks='' group='unknown' job='unknown'
    data=$(/usr/bin/systemctl show "$unit" --property=LoadState --property=ActiveState \
        --property=SubState --property=MainPID --property=ControlPID --property=TasksCurrent --property=ControlGroup --property=Job 2>/dev/null) ||
        fail 'Could not read recovery service state.'
    while IFS='=' read -r key value; do
        case "$key" in
            LoadState) load=$value;; ActiveState) active=$value;; SubState) sub=$value;;
            MainPID) pid=$value;; ControlPID) control=$value;; TasksCurrent) tasks=$value;; ControlGroup) group=$value;; Job) job=$value;;
            *) fail 'Unexpected recovery service properties.';;
        esac
        count=$((count+1))
    done <<< "$data"
    [[ "$count" == 8 && -z "$job" && "$pid" == 0 && "$control" == 0 && ( "$tasks" == 0 || ( "$tasks" == '[not set]' && -z "$group" ) ) ]] ||
        fail 'Recovery requires no service processes or jobs.'
    if [[ "$optional" == yes && "$load" == not-found && "$active" == inactive && "$sub" == dead ]]; then return; fi
    [[ "$load" == loaded && ( ( "$active" == inactive && "$sub" == dead ) || ( "$active" == failed && "$sub" == failed ) ) ]] ||
        fail 'Recovery requires an unambiguously stopped service.'
}

recovery_preflight() {
    stage=recovery_preflight
    [[ $(git_cmd rev-parse HEAD) == "$expected" ]] || fail 'Expected current SHA does not match checkout.'
    [[ "$target" != "$expected" ]] || fail 'Recovery requires a new forward commit.'
    recovery_idle
}

recovery_idle() {
    local timer
    stopped_unit "$UNIT"
    stopped_unit tdb-daily-backup.service yes
    stopped_unit tdb-backup-recover.service yes
    timer=$(/usr/bin/systemctl show tdb-daily-backup.timer --property=LoadState --property=ActiveState \
        --property=SubState --property=UnitFileState --property=Job 2>/dev/null) || fail 'Could not read backup timer.'
    # Compare whole normalized snapshot, including an explicitly empty Job.
    timer=$(printf '%s\n' "$timer" | LC_ALL=C sort)
    [[ "$timer" == $'ActiveState=inactive\nJob=\nLoadState=not-found\nSubState=dead\nUnitFileState=' ||
       "$timer" == $'ActiveState=inactive\nJob=\nLoadState=loaded\nSubState=dead\nUnitFileState=disabled' ]] ||
        fail 'Disable and settle the backup timer before recovery.'
    # Do not import admin code, create state, delete owner, or print JSON values.
    /usr/bin/python3 -I -S -B - "$RECOVERY_CONFIG" "$RECOVERY_STATE" "$STATE_DIR/deploy.lock" "$RECOVERY_SOCKET" <<'PY' || fail 'Maintenance state is unsafe, busy or unresolved.'
import fcntl, json, os, stat, sys
from pathlib import Path
def private(path, directory=False):
    for parent in (path, *path.parents):
        if parent.is_symlink(): raise ValueError()
    s = path.stat()
    if s.st_uid != os.getuid() or s.st_mode & 0o077: raise ValueError()
    if directory != stat.S_ISDIR(s.st_mode): raise ValueError()
    if not directory and (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1): raise ValueError()
    return s
def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError()
        result[key] = value
    return result
def read(path):
    before = private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        after = os.fstat(f.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino): raise ValueError()
        data = f.read(65537)
    if len(data) > 65536: raise ValueError()
    return json.loads(data, object_pairs_hook=unique)
try:
    config, state, lock, socket = map(Path, sys.argv[1:])
    for path in (config, state, lock, socket):
        if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError()
    marker = socket.with_name(socket.name+'.committed')
    if marker.exists() or marker.is_symlink(): raise ValueError()
    info = private(lock)
    held = os.fstat(9)
    if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino): raise ValueError()
    if config.exists() or config.is_symlink():
        private(config.parent, True)
        c = read(config)
        if not isinstance(c, dict) or c.get('state_dir', str(state)) != str(state) or c.get('service', 'tdb.service') != 'tdb.service' or c.get('deploy_lock', str(lock)) != str(lock) or c.get('socket', str(socket)) != str(socket): raise ValueError()
        private(state, True)
    if state.exists() or state.is_symlink():
        # Refuse orphan/custom state: its binding cannot be established here.
        if not config.exists(): raise ValueError()
        private(state, True)
        lease = state/'offsite.lock'
        before = private(lease)
        with os.fdopen(os.open(lease, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as f:
            after = os.fstat(f.fileno())
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino): raise ValueError()
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in ('owner', 'hold'):
                path = state/(name+'.json')
                if not path.exists() and not path.is_symlink(): continue
                record = read(path)
                if not isinstance(record, dict) or set(record) != {'format','value'} or record['format'] != 'tdb-daily-state-v1': raise ValueError()
                if name == 'owner' and record['value'] is not None: raise ValueError()
                if name == 'hold' and record['value'] is not False: raise ValueError()
except Exception:
    sys.exit(1)
PY
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
    if (( emergency )); then
        [[ "$previous" == "$expected" ]] || fail 'Expected current SHA changed.'
    fi
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
    if (( emergency )); then
        recovery_preflight
    else
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
    fi
    update_and_start "$previous_invocation"
}

update_and_start() {
    local previous_invocation=$1 deadline invocation pid restarts dirty
    stage=update
    timeout 60 git -c core.hooksPath=/dev/null merge --ff-only --no-edit --no-overwrite-ignore "$target" >/dev/null 2>&1 || fail 'Fast-forward update failed; no reset/clean will be attempted.'
    dirty=$(git_cmd status --porcelain --untracked-files=no 2>/dev/null) || fail 'Could not inspect the updated checkout.'
    [[ $(git_cmd rev-parse HEAD) == "$target" && -z "$dirty" ]] || fail 'Updated checkout is not clean at the selected commit.'
    stage=imports
    timeout 60 "$PROJECT/.venv/bin/python" -m compileall -q src >/dev/null 2>&1 || fail 'New source compilation failed.'
    timeout 60 "$PROJECT/.venv/bin/python" -c 'import src.main' >/dev/null 2>&1 || fail 'New source imports failed.'
    stage=start
    if (( emergency )); then recovery_idle; stage=start; fi
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
    if (( emergency )); then
        [[ $(property ActiveState) == active && $(property SubState) == running && $(property InvocationID) == "$invocation" && $(property MainPID) == "$pid" && $(property NRestarts) == "$restarts" ]] || fail 'Recovery process identity changed.'
    fi
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
    elif [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^(check-recovery|recover)\ ([0-9a-f]{40})\ ([0-9a-f]{40})$ ]]; then
        mode=${BASH_REMATCH[1]}
        expected=${BASH_REMATCH[2]}
        target=${BASH_REMATCH[3]}
        emergency=1
    else
        fail 'Allowed commands: check, deploy SHA, check-recovery EXPECTED TARGET, recover EXPECTED TARGET.'
    fi
    trap finish EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
    if (( emergency )); then
        [[ -f "$STATE_DIR/deploy.lock" && ! -L "$STATE_DIR/deploy.lock" ]] || fail 'Recovery requires the existing deployment lock.'
    fi
    [[ ! -L "$STATE_DIR" ]] || fail 'The state directory must not be a symlink.'
    mkdir -p "$STATE_DIR"
    [[ $(cd -- "$STATE_DIR" && pwd -P) == "$STATE_DIR" ]] || fail 'The state directory must use its physical path.'
    chmod 700 "$STATE_DIR"
    [[ ! -L "$STATE_DIR/deploy.lock" ]] || fail 'The lock must not be a symlink.'
    exec 9>>"$STATE_DIR/deploy.lock"
    flock -n 9 || fail 'Another server deployment/check holds the lock.'
    preflight
    if [[ "$mode" == check ]]; then
        printf 'CHECK_OK protocol=1\n'
    elif [[ "$mode" == check-recovery ]]; then
        check_target
        recovery_preflight
        printf 'RECOVERY_CHECK_OK %s %s\n' "$expected" "$target"
    else
        deploy
    fi
}

main "$@"
