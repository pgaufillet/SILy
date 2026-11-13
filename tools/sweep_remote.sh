#!/bin/sh
# Run a sweep on a remote Linux machine reached by ssh, detached from the
# ssh session: the runs of the large-network suite (`tests/large.py
# sweep-plan`, each one `record`ed) or ranges of seed shifts of the
# scenario suites (`tests/seeds.py --json`), for one or several source
# trees (variants), many at a time.
#
#   tools/sweep_remote.sh start HOST [LABEL=DIR ...]
#       copy src/ sim/ tests/ tools/ of this tree (label `current`) and of
#       each DIR (label LABEL, e.g. another worktree) into REMOTE_DIR/LABEL
#       on HOST, with the local MicroPython unix port binary (and
#       micropython-lib's unittest for a seeds sweep); check that the
#       remote binary computes what the local one does (the `make
#       simbench` digest and the report of one run, both printed); write
#       the plan (every run of the sweep for every label, the same
#       options for all) and start it under nohup, JOBS runs at a time,
#       each under `timeout 3600`, with SWEEP_VARIANT the label and
#       SWEEP_REV the tree's `git describe` computed here. The records go
#       to REMOTE_DIR/out/LABEL; a run recorded there is skipped (out/ is
#       never wiped: start again to resume an interrupted sweep).
#   tools/sweep_remote.sh fetch HOST
#       say how far the sweep is (records against plan lines); once it
#       has ended, copy out/ into FETCH, print the failure rates
#       (`large.py aggregate`, `seeds.py aggregate`) and the failed jobs,
#       and exit with the sweep's status
#
# Environment: SWEEP, the plan's options (large: those of `large.py
# sweep-plan`; seeds: `[MODULE...] [--class NAME]...`), KIND (large, the
# default, or seeds), SHIFTS (seeds: FIRST-LAST, default 0-99), CHUNK
# (seeds: shifts per job, default 10), JOBS (default 32), REMOTE_DIR
# (default sily-sweep, relative to the remote home), FETCH (default
# build/sweep-remote of the repository), MICROPYTHON (the local unix port
# binary), MPY_LIB (the local micropython-lib), REMOTE_MICROPYTHON (a unix
# port built on HOST, used instead of copying MICROPYTHON).
#
# HOST: Ubuntu 24.04 or Debian 13 (the local binary is dynamically linked:
# glibc 2.38 or later and libffi.so.8), with rsync; e.g. a 32-vCPU
# instance with JOBS=32. Elsewhere, build the unix port there (MicroPython
# v1.29.0, the version of the local one) and pass REMOTE_MICROPYTHON:
#
#   apt install build-essential pkg-config libffi-dev git
#   git clone --depth 1 --branch v1.29.0 https://github.com/micropython/micropython
#   make -C micropython/ports/unix submodules
#   make -C micropython/mpy-cross
#   make -C micropython/ports/unix
#   (REMOTE_MICROPYTHON=$HOME/micropython/ports/unix/build-standard/micropython)

set -u
cd "$(dirname "$0")/.." || exit 2

T3S3=${T3S3:-$HOME/git/t3s3-micropython}
MICROPYTHON=${MICROPYTHON:-$T3S3/micropython/ports/unix/build-standard/micropython}
MPY_LIB=${MPY_LIB:-$T3S3/micropython/lib/micropython-lib}
REMOTE_MICROPYTHON=${REMOTE_MICROPYTHON:-}
REMOTE_DIR=${REMOTE_DIR:-sily-sweep}
FETCH=${FETCH:-build/sweep-remote}
case "$FETCH" in /*) ;; *) FETCH=$PWD/$FETCH ;; esac
JOBS=${JOBS:-32}
KIND=${KIND:-large}
SWEEP=${SWEEP:-}
SHIFTS=${SHIFTS:-0-99}
CHUNK=${CHUNK:-10}
HEAP=32M
CHECK_RUN="forming course 1 14"  # the run whose report is compared

usage() {
    echo "usage: $0 start HOST [LABEL=DIR ...] | fetch HOST" >&2
    exit 2
}

[ $# -ge 2 ] || usage
cmd=$1 host=$2
shift 2

# The local interpreter on a tree's scripts: mp TREE SCRIPT ARGS...
mp() {
    tree=$1
    shift
    (cd "$tree" && MICROPYPATH=".frozen:$PWD/src:$PWD:$MPY_LIB/python-stdlib/unittest" \
        "$MICROPYTHON" -X heapsize=$HEAP "$@")
}

# State of the remote sweep: done, running or none.
state() {
    ssh "$host" "cd '$REMOTE_DIR' 2>/dev/null || { echo none; exit; }
        if [ -f status ]; then echo done
        elif [ -f pid ] && kill -0 \$(cat pid) 2>/dev/null; then echo running
        else echo none; fi"
}

# Records written so far on the remote side, and plan lines.
progress() {
    ssh "$host" "cd '$REMOTE_DIR' && echo \$(find out -name '*.json' -o -name 'seeds_*.jsonl' \
        2>/dev/null | wc -l) of \$(wc -l <plan.txt) runs recorded"
}

# The equivalence check's output of a tree: simbench digest, run report
# without its wall-clock time.
equivalence() {
    "$@" tools/simbench.py | grep '^digest '
    # shellcheck disable=SC2086
    "$@" tests/large.py run - $CHECK_RUN | sed 's/, [0-9.]* s$//'
}

# The plan's lines for one label: large, `LABEL SCENARIO TOPOLOGY SEED
# NODES LOAD`; seeds, `LABEL FIRST COUNT OPTIONS`.
plan() {
    label=$1
    if [ "$KIND" = seeds ]; then
        first=${SHIFTS%-*} last=${SHIFTS#*-}
        mp . tests/seeds.py plan $((last - first + 1)) "$CHUNK" --first "$first" |
            while read -r k n; do
                echo "$label $k $n${SWEEP:+ $SWEEP}"  # no trailing blank (xargs -L)
            done
    else
        sed "s/^/$label /" "$tmp/plan.lst"
    fi
}

case "$cmd" in
start)
    case "$KIND" in large | seeds) ;; *) echo "KIND: large or seeds" >&2; exit 2 ;; esac
    [ -n "$REMOTE_MICROPYTHON" ] || [ -x "$MICROPYTHON" ] ||
        { echo "MicroPython not found: set MICROPYTHON" >&2; exit 2; }
    labels="current" dirs="$PWD"
    for arg in "$@"; do
        label=${arg%%=*} dir=${arg#*=}
        case "$label" in
        "" | out | bin | lib | *[!A-Za-z0-9._-]*) echo "bad label: $arg" >&2; exit 2 ;;
        esac
        case " $labels " in *" $label "*) echo "label $label twice" >&2; exit 2 ;; esac
        [ -f "$dir/tests/large.py" ] || { echo "$dir: not a source tree" >&2; exit 2; }
        labels="$labels $label" dirs="$dirs $(cd "$dir" && pwd)"
    done
    if [ "$(state)" = running ]; then
        echo "a sweep is still going on $host:$REMOTE_DIR" >&2
        exit 1
    fi
    tmp=$(mktemp -d) || exit 2
    trap 'rm -rf "$tmp"' EXIT
    if [ "$KIND" = large ]; then
        # shellcheck disable=SC2086
        mp . tests/large.py sweep-plan $SWEEP >"$tmp/plan.lst" ||
            { cat "$tmp/plan.lst" >&2; exit 2; }
    fi
    set -- $dirs
    for label in $labels; do
        plan "$label" >"$tmp/$label.plan" || exit 2
        git -C "$1" describe --always --dirty --exclude='*' 2>/dev/null >"$tmp/$label.rev" ||
            echo unknown >"$tmp/$label.rev"
        shift
    done
    # Each run for every label in turn: the largest runs first for all.
    # shellcheck disable=SC2046
    (cd "$tmp" && paste -d '\n' $(for label in $labels; do echo "$label.plan"; done)) \
        >"$tmp/plan.txt"
    cat >"$tmp/run1.sh" <<'EOF'
#!/bin/sh
# One job of the sweep, from the sweep directory: run1.sh KIND LABEL ARGS...
kind=$1 label=$2
shift 2
top=$PWD out=$PWD/out/$label
mkdir -p "$out" && cd "$top/$label" || exit 1
export MICROPYPATH=".frozen:$PWD/src:$PWD:$top/lib"
export SWEEP_VARIANT="$label" SWEEP_REV="$(cat REV)"
if [ "$kind" = seeds ]; then
    first=$1 count=$2
    shift 2
    file=$out/seeds_$first.jsonl
    [ -f "$file" ] && exit 0
    timeout 3600 "$top/bin/micropython" -X heapsize=32M tests/seeds.py "$count" \
        --first "$first" --json "$@" >"$file.tmp"
    status=$?
    # 1: a shift failed, recorded in the lines
    [ $status -le 1 ] && mv "$file.tmp" "$file" && exit 0
else
    timeout 3600 "$top/bin/micropython" -X heapsize=32M tests/large.py record "$out" "$@"
    status=$?
    [ $status -eq 0 ] && exit 0
fi
echo "$label $*: exit status $status" >>"$top/errors.log"
exit 1
EOF
    echo "$KIND" >"$tmp/kind"
    echo "copying to $host:$REMOTE_DIR: $labels"
    ssh "$host" "mkdir -p '$REMOTE_DIR/bin' '$REMOTE_DIR/lib'" || exit
    if [ -n "$REMOTE_MICROPYTHON" ]; then
        ssh "$host" "ln -sf '$REMOTE_MICROPYTHON' '$REMOTE_DIR/bin/micropython'" || exit
    else
        rsync -a "$MICROPYTHON" "$host:$REMOTE_DIR/bin/micropython" || exit
    fi
    if [ "$KIND" = seeds ]; then
        rsync -a --delete "$MPY_LIB/python-stdlib/unittest/unittest" "$host:$REMOTE_DIR/lib/" ||
            exit
    fi
    set -- $dirs
    for label in $labels; do
        rsync -a --delete --exclude __pycache__ "$1/src" "$1/sim" "$1/tests" "$1/tools" \
            "$host:$REMOTE_DIR/$label/" &&
            rsync -a "$tmp/$label.rev" "$host:$REMOTE_DIR/$label/REV" || exit
        shift
    done
    rsync -a "$tmp/plan.txt" "$tmp/run1.sh" "$tmp/kind" "$host:$REMOTE_DIR/" || exit
    echo "equivalence check (simbench digest, report of $CHECK_RUN):"
    equivalence mp . >"$tmp/local.txt"
    # shellcheck disable=SC2016
    equivalence ssh "$host" "cd '$REMOTE_DIR/current' &&" \
        'MICROPYPATH=".frozen:$PWD/src:$PWD:$PWD/../lib"' \
        ../bin/micropython -X heapsize=$HEAP >"$tmp/remote.txt"
    echo "--- local"
    cat "$tmp/local.txt"
    echo "--- $host"
    cat "$tmp/remote.txt"
    if ! [ -s "$tmp/local.txt" ] || ! cmp -s "$tmp/local.txt" "$tmp/remote.txt"; then
        echo "the remote interpreter does not compute what the local one does: not started" >&2
        exit 1
    fi
    ssh "$host" "cd '$REMOTE_DIR' && rm -f status pid run.log errors.log || exit
        nohup sh -c 'xargs -P $JOBS -L 1 sh run1.sh $KIND <plan.txt; echo \$? >status' \
            >run.log 2>&1 </dev/null & echo \$! >pid" || exit
    echo "started on $host:$REMOTE_DIR: $(wc -l <"$tmp/plan.txt") runs, $JOBS at a time"
    echo "follow it:  ssh $host tail -f $REMOTE_DIR/run.log"
    echo "fetch it:   make sweep-remote-fetch SWEEP_HOST=$host"
    ;;
fetch)
    case "$(state)" in
    running)
        echo "still running on $host:$REMOTE_DIR: $(progress)"
        ssh "$host" "tail -3 '$REMOTE_DIR/run.log'"
        exit 0
        ;;
    none)
        echo "no sweep on $host:$REMOTE_DIR" >&2
        exit 2
        ;;
    esac
    mkdir -p "$FETCH" &&
        rsync -a --delete "$host:$REMOTE_DIR/out/" "$FETCH/out/" &&
        rsync -a "$host:$REMOTE_DIR/run.log" "$host:$REMOTE_DIR/status" \
            "$host:$REMOTE_DIR/plan.txt" "$host:$REMOTE_DIR/kind" "$FETCH/" || exit
    rm -f "$FETCH/errors.log"
    rsync -a "$host:$REMOTE_DIR/errors.log" "$FETCH/" 2>/dev/null
    if [ "$(cat "$FETCH/kind")" = seeds ]; then
        mp . tests/seeds.py aggregate "$FETCH"/out/*
    else
        mp . tests/large.py aggregate "$FETCH"/out/*
    fi
    echo "fetched into $FETCH: $(progress)"
    if [ -s "$FETCH/errors.log" ]; then
        echo "failed jobs (crash or timeout, no record; start again to retry them):"
        cat "$FETCH/errors.log"
    fi
    status=$(cat "$FETCH/status")
    echo "remote exit status: $status"
    exit "$status"
    ;;
*) usage ;;
esac
