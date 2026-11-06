#!/bin/sh
# Run the TLC models on a remote Linux machine reached by ssh (Java 11 or
# later and rsync installed there), detached from the ssh session.
#
#   spec/remote.sh start HOST [NAMES...]
#       copy spec/ and tla2tools.jar to REMOTE_DIR on HOST and start
#       `run.sh NAMES` there (default all) under nohup; its output goes to
#       REMOTE_DIR/run.log, its exit status to REMOTE_DIR/status
#   spec/remote.sh fetch HOST
#       say whether the run is still going; once it has ended, copy its
#       output into FETCH (run.log, status, out/: TLC logs and traces),
#       merge the durations it recorded into durations.txt, print the
#       summary and exit with the run's status
#
# Environment: TLA_JAR (local tla2tools.jar), REMOTE_DIR (default
# sily-spec, relative to the remote home), FETCH (default build/spec-remote
# of the repository), and for start JOBS (default 8), WORKERS, HEAP,
# MAXSECS, QUICK_SECS, MACHINE, passed to run.sh.

set -u
cd "$(dirname "$0")/.." || exit 2

TLA_JAR=${TLA_JAR:-$HOME/.local/lib/tla2tools.jar}
REMOTE_DIR=${REMOTE_DIR:-sily-spec}
FETCH=${FETCH:-build/spec-remote}
case "$FETCH" in /*) ;; *) FETCH=$PWD/$FETCH ;; esac
JOBS=${JOBS:-8}

usage() {
    echo "usage: $0 start HOST [NAMES...] | fetch HOST" >&2
    exit 2
}

[ $# -ge 2 ] || usage
cmd=$1 host=$2
shift 2

# State of the remote run: done, running or none.
state() {
    ssh "$host" "cd '$REMOTE_DIR' 2>/dev/null || { echo none; exit; }
        if [ -f status ]; then echo done
        elif [ -f pid ] && kill -0 \$(cat pid) 2>/dev/null; then echo running
        else echo none; fi"
}

case "$cmd" in
start)
    [ -f "$TLA_JAR" ] || { echo "tla2tools.jar not found: set TLA_JAR" >&2; exit 2; }
    if [ "$(state)" = running ]; then
        echo "a run is still going on $host:$REMOTE_DIR" >&2
        exit 1
    fi
    env="JOBS=$JOBS"
    for var in WORKERS HEAP MAXSECS QUICK_SECS MACHINE; do
        eval "value=\${$var:-}"
        [ -z "$value" ] || env="$env $var=$value"
    done
    ssh "$host" "mkdir -p '$REMOTE_DIR'" &&
        rsync -a --delete spec/ "$host:$REMOTE_DIR/spec/" &&
        rsync -a "$TLA_JAR" "$host:$REMOTE_DIR/tla2tools.jar" &&
        ssh "$host" "cd '$REMOTE_DIR' && rm -rf out run.log status pid || exit
            $env TLA_JAR=\$PWD/tla2tools.jar OUT=\$PWD/out \
            nohup sh -c 'sh spec/run.sh \"\$@\"; echo \$? >status' run.sh ${*:-all} \
            >run.log 2>&1 </dev/null & echo \$! >pid" || exit
    echo "started on $host:$REMOTE_DIR ($env)"
    echo "follow it:  ssh $host tail -f $REMOTE_DIR/run.log"
    echo "fetch it:   make spec-remote-fetch SPEC_HOST=$host"
    ;;
fetch)
    case "$(state)" in
    running)
        echo "still running on $host:$REMOTE_DIR:" \
            "$(ssh "$host" "grep -cE '^(ok|UNEXPECTED|TIMEOUT) ' '$REMOTE_DIR/run.log'")" \
            "configurations done"
        ssh "$host" "tail -3 '$REMOTE_DIR/run.log'"
        exit 0
        ;;
    none)
        echo "no run on $host:$REMOTE_DIR" >&2
        exit 2
        ;;
    esac
    mkdir -p "$FETCH" &&
        rsync -a --delete "$host:$REMOTE_DIR/out/" "$FETCH/out/" &&
        rsync -a "$host:$REMOTE_DIR/run.log" "$host:$REMOTE_DIR/status" "$FETCH/" || exit
    sh spec/run.sh record "$FETCH/out/durations.d"
    status=$(cat "$FETCH/status")
    results=$(grep -cE '^(ok|UNEXPECTED|TIMEOUT) ' "$FETCH/run.log")
    echo "fetched into $FETCH: $results results, durations merged into spec/durations.txt"
    grep -E '^(UNEXPECTED|TIMEOUT) ' "$FETCH/run.log"
    echo "remote exit status: $status"
    exit "$status"
    ;;
*) usage ;;
esac
