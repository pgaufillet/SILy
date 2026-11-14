#!/bin/sh
# Run a verification campaign on a remote Linux machine reached by ssh,
# detached from the ssh session, then fetch its records and judge them.
#
#   tools/campaign.sh start HOST [LABEL=DIR ...]
#       expand the plan PLAN here into its jobs (`tools/campaign.py plan
#       --list`: the longest first, from the time table TIMES); copy src/
#       sim/ tests/ tools/ of this tree (label `current`) and of each DIR
#       (label LABEL, e.g. another worktree) into REMOTE_DIR/LABEL on
#       HOST, with the local MicroPython unix port binary and
#       micropython-lib's unittest; check that the remote binary computes
#       what the local one does (the `make simbench` digest and the report
#       of one run, both printed); then start, under nohup:
#       - the plan's spec job, if any, by `spec/remote.sh start` into
#         REMOTE_DIR/spec, at normal priority, SPEC_JOBS configurations at
#         a time (the models of this tree; not started again once it has
#         ended there: remove REMOTE_DIR/spec to run it again);
#       - every other job for every label in one pool under `nice -n 10`,
#         JOBS at a time, each job for every label in turn, the longest
#         first, each under `timeout 3600` with SWEEP_VARIANT the label and
#         SWEEP_REV the tree's `git describe` computed here: the large runs
#         recorded (`tests/large.py record`) into REMOTE_DIR/out/LABEL/large,
#         the JSON lines of each seeds chunk (`tests/seeds.py --json`) into
#         REMOTE_DIR/out/LABEL/seeds/seeds_FIRST.jsonl.
#       A job recorded there is skipped: out/ is never wiped, start again
#       to resume an interrupted campaign or to retry its failed jobs.
#       Refused while the pool or the spec job runs, and when REMOTE_DIR
#       holds a label at another revision (fetch it, then remove REMOTE_DIR
#       there or choose another).
#   tools/campaign.sh fetch HOST
#       while the pool or the spec job runs, how far they are; once both
#       have ended, copy into FETCH out/ (the records), the pool's run.log
#       and errors.log (its failed jobs: timeout or crash, no record), the
#       plan (plan.txt), the job list (jobs.txt), the labels' revisions
#       (revs.txt); then write there, each printed:
#       - spec.txt: `spec/remote.sh fetch` of the spec job (its output in
#         spec/, its durations merged into spec/durations.txt);
#       - times.txt: the run times of the label `current` merged into
#         TIMES (`campaign.py times`, the share of a job: the vCPUs of HOST
#         per pool job, at most 1);
#       - records-REV.tar.xz: out/, plan.txt, jobs.txt, revs.txt and the
#         spec job's run.log, REV the revision of `current`;
#       - bounds-LABEL.txt: `campaign.py bounds` of each label, with
#         --expect the plan's seeds shifts;
#       - compare-LABEL.txt, when BASELINE is set (the records of an
#         earlier campaign: a directory, or a .tar.xz extracted into
#         FETCH/baseline): `campaign.py compare` of the label
#         BASELINE_LABEL of BASELINE against each label, with the options
#         COMPARE (e.g. --exact for a change meant to keep behaviour);
#       - verdicts.txt: the status of each, the worst the exit status: 0
#         all passed; 1 a breach, a difference or a TLC outcome not the
#         expected one; 2 a job without record, a missing seeds shift or an
#         input problem.
#
# The spec job runs at a higher priority than the pool: the run times of
# the jobs that ran beside it are longer than those of the pool alone, and
# the time table counts them slower than they are (a conservative cost).
# So does it the jobs of an earlier start resumed with another JOBS.
#
# Environment: PLAN (default tools/campaign.txt), TIMES (default
# tools/campaign_times.txt), JOBS (pool jobs at a time, default the vCPUs
# of HOST), SPEC_JOBS (default 8; TLA_JAR, WORKERS, HEAP, MAXSECS go to
# spec/remote.sh, DURATIONS to spec/run.sh), REMOTE_DIR (default
# sily-campaign, relative to the remote home), FETCH (default
# build/campaign), BASELINE, BASELINE_LABEL (default current), COMPARE,
# MICROPYTHON (the local unix port binary), MPY_LIB (the local
# micropython-lib), REMOTE_MICROPYTHON (a unix port built on HOST, used
# instead of copying MICROPYTHON). Relative paths are relative to the
# repository.
#
# HOST: Linux with rsync, and Java 11 or later for a spec job (apt install
# rsync default-jre-headless); the local binary is dynamically linked:
# Ubuntu 24.04 or Debian 13 (glibc 2.38 or later and libffi.so.8). e.g. a
# 32-vCPU instance with JOBS=32. Elsewhere, build the unix port there
# (MicroPython v1.29.0, the version of the local one) and pass
# REMOTE_MICROPYTHON:
#
#   apt install build-essential pkg-config libffi-dev git
#   git clone --depth 1 --branch v1.29.0 https://github.com/micropython/micropython
#   make -C micropython/ports/unix submodules
#   make -C micropython/mpy-cross
#   make -C micropython/ports/unix
#   (REMOTE_MICROPYTHON=$HOME/micropython/ports/unix/build-standard/micropython)
#
# Here: ssh, rsync, tar with xz.

set -u
cd "$(dirname "$0")/.." || exit 2

T3S3=${T3S3:-$HOME/git/t3s3-micropython}
MICROPYTHON=${MICROPYTHON:-$T3S3/micropython/ports/unix/build-standard/micropython}
MPY_LIB=${MPY_LIB:-$T3S3/micropython/lib/micropython-lib}
REMOTE_MICROPYTHON=${REMOTE_MICROPYTHON:-}
PLAN=${PLAN:-tools/campaign.txt}
TIMES=${TIMES:-tools/campaign_times.txt}
JOBS=${JOBS:-}
SPEC_JOBS=${SPEC_JOBS:-8}
REMOTE_DIR=${REMOTE_DIR:-sily-campaign}
FETCH=${FETCH:-build/campaign}
case "$FETCH" in /*) ;; *) FETCH=$PWD/$FETCH ;; esac
BASELINE=${BASELINE:-}
case "$BASELINE" in "" | /*) ;; *) BASELINE=$PWD/$BASELINE ;; esac
BASELINE_LABEL=${BASELINE_LABEL:-current}
COMPARE=${COMPARE:-}
MP_HEAP=32M
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
        "$MICROPYTHON" -X heapsize=$MP_HEAP "$@")
}

# State of a detached run (the pool, or spec/remote.sh's) in the remote
# directory $1: done, running, stopped (ended without status) or none.
state() {
    ssh "$host" "cd '$1' 2>/dev/null || { echo none; exit; }
        if [ -f status ]; then echo done
        elif [ ! -f pid ]; then echo none
        elif kill -0 \$(cat pid) 2>/dev/null; then echo running
        else echo stopped; fi"
}

# The pool's jobs recorded, in all and failed.
progress() {
    ssh "$host" "cd '$REMOTE_DIR' && sh run1.sh count"
}

# The equivalence check's output of a tree: simbench digest, run report
# without its wall-clock time.
equivalence() {
    "$@" tools/simbench.py | grep '^digest '
    # shellcheck disable=SC2086
    "$@" tests/large.py run - $CHECK_RUN | sed 's/, [0-9.]* s$//'
}

# The pool's job script, run on HOST from REMOTE_DIR.
run1() {
    cat <<'EOF'
#!/bin/sh
# A job of the campaign's pool, from the campaign directory:
#   sh run1.sh LABEL large SCENARIO TOPOLOGY SEED NODES LOAD
#   sh run1.sh LABEL seeds FIRST COUNT [MODULE...] [--class NAME]...
# skipped when its record exists (out/LABEL/large/NAME.json, or
# out/LABEL/seeds/seeds_FIRST.jsonl); a failure (timeout, crash) is
# appended to errors.log. `sh run1.sh count`: the pool's jobs recorded.
set -f

# Set file to the record of the job LABEL KIND ARGS...
locate() {
    if [ "$2" = large ]; then
        file=out/$1/large/$3_$4_$5_$6_$7.json
    else
        file=out/$1/seeds/seeds_$3.jsonl
    fi
}

if [ "$1" = count ]; then
    recorded=0 total=0 failed=0
    while read -r line; do
        locate $line
        total=$((total + 1))
        [ -f "$file" ] && recorded=$((recorded + 1))
    done <pool.txt
    [ -f errors.log ] && failed=$(($(wc -l <errors.log)))
    echo "$recorded of $total pool jobs recorded, $failed failed"
    exit 0
fi
job="$*"
locate "$@"
[ -f "$file" ] && exit 0
label=$1 kind=$2
shift 2
top=$PWD
mkdir -p "out/$label/$kind" && cd "$label" || exit 1
export MICROPYPATH=".frozen:$PWD/src:$PWD:$top/lib"
export SWEEP_VARIANT="$label" SWEEP_REV="$(cat REV)"
if [ "$kind" = seeds ]; then
    first=$1 count=$2
    shift 2
    timeout 3600 "$top/bin/micropython" -X heapsize=32M tests/seeds.py "$count" \
        --first "$first" --json "$@" >"$top/$file.tmp"
    status=$?
    # 1: a shift failed, recorded in the lines
    [ $status -le 1 ] && mv "$top/$file.tmp" "$top/$file" && exit 0
else
    timeout 3600 "$top/bin/micropython" -X heapsize=32M tests/large.py record \
        "$top/out/$label/large" "$@"
    status=$?
    [ $status -eq 0 ] && exit 0
fi
echo "$job: exit status $status" >>"$top/errors.log"
exit 1
EOF
}

start() {
    [ -x "$MICROPYTHON" ] || { echo "MicroPython not found: set MICROPYTHON" >&2; exit 2; }
    labels="current" dirs="$PWD"
    for arg in "$@"; do
        case "$arg" in *=*) ;; *) echo "not LABEL=DIR: $arg" >&2; exit 2 ;; esac
        label=${arg%%=*} dir=${arg#*=}
        case "$label" in
        "" | out | bin | lib | spec | pid | status | *[!A-Za-z0-9_-]*)
            echo "bad label: $arg" >&2
            exit 2
            ;;
        esac
        case " $labels " in *" $label "*) echo "label $label twice" >&2; exit 2 ;; esac
        [ -f "$dir/tests/large.py" ] || { echo "$dir: not a source tree" >&2; exit 2; }
        labels="$labels $label" dirs="$dirs $(cd "$dir" && pwd)"
    done
    for d in "$REMOTE_DIR" "$REMOTE_DIR/spec"; do
        if [ "$(state "$d")" = running ]; then
            echo "a campaign is still running on $host:$REMOTE_DIR" >&2
            exit 1
        fi
    done
    tmp=$(mktemp -d) || exit 2
    trap 'rm -rf "$tmp"' EXIT
    # shellcheck disable=SC2086
    set -- $dirs
    for label in $labels; do
        rev=$(git -C "$1" describe --always --dirty --exclude='*' 2>/dev/null) || rev=unknown
        echo "$rev" >"$tmp/$label.rev"
        echo "$label $rev" >>"$tmp/revs.txt"
        shift
    done
    ssh "$host" "cat '$REMOTE_DIR/revs.txt' 2>/dev/null" >"$tmp/old.txt"
    conflicts=$(awk 'NR == FNR { rev[$1] = $2; next }
        ($1 in rev) && rev[$1] != $2 { printf "%s%s %s there, %s here", s, $1, $2, rev[$1]; s = "; " }' \
        "$tmp/revs.txt" "$tmp/old.txt")
    if [ -n "$conflicts" ]; then
        echo "$host:$REMOTE_DIR holds a campaign of other revisions ($conflicts):" \
            "fetch it, then remove that directory or set REMOTE_DIR" >&2
        exit 1
    fi
    mp . tools/campaign.py plan "$PLAN" --times "$TIMES" --list >"$tmp/jobs.txt" || exit 2
    if ! mp . tools/campaign.py plan "$PLAN" --times "$TIMES" --expect >"$tmp/shifts.txt"; then
        echo "bounds will run without --expect" >&2
        : >"$tmp/shifts.txt"
    fi
    cp "$PLAN" "$tmp/plan.txt" || exit 2
    # Each job for every label in turn: the longest first for all.
    grep -v '^spec ' "$tmp/jobs.txt" | while read -r line; do
        for label in $labels; do
            echo "$label $line"
        done
    done >"$tmp/pool.txt"
    names=$(sed -n 's/^spec //p' "$tmp/jobs.txt")
    if [ -n "$names" ]; then
        [ -f "${TLA_JAR:-$HOME/.local/lib/tla2tools.jar}" ] ||
            { echo "tla2tools.jar not found: set TLA_JAR" >&2; exit 2; }
        ssh "$host" "command -v java >/dev/null" ||
            { echo "no java on $host (apt install default-jre-headless)" >&2; exit 1; }
    fi
    vcpus=$(ssh "$host" nproc) || exit
    jobs=${JOBS:-$vcpus}
    echo "$vcpus $jobs" >"$tmp/machine.txt"
    run1 >"$tmp/run1.sh"

    echo "copying to $host:$REMOTE_DIR: $labels"
    ssh "$host" "mkdir -p '$REMOTE_DIR/bin' '$REMOTE_DIR/lib' '$REMOTE_DIR/out'" || exit
    if [ -n "$REMOTE_MICROPYTHON" ]; then
        ssh "$host" "ln -sf '$REMOTE_MICROPYTHON' '$REMOTE_DIR/bin/micropython'" || exit
    else
        rsync -a "$MICROPYTHON" "$host:$REMOTE_DIR/bin/micropython" || exit
    fi
    rsync -a --delete "$MPY_LIB/python-stdlib/unittest/unittest" "$host:$REMOTE_DIR/lib/" || exit
    # shellcheck disable=SC2086
    set -- $dirs
    for label in $labels; do
        rsync -a --delete --exclude __pycache__ "$1/src" "$1/sim" "$1/tests" "$1/tools" \
            "$host:$REMOTE_DIR/$label/" &&
            rsync -a "$tmp/$label.rev" "$host:$REMOTE_DIR/$label/REV" || exit
        shift
    done
    (cd "$tmp" && rsync -a plan.txt jobs.txt pool.txt revs.txt shifts.txt machine.txt run1.sh \
        "$host:$REMOTE_DIR/") || exit
    echo "equivalence check (simbench digest, report of $CHECK_RUN):"
    equivalence mp . >"$tmp/local.txt"
    # shellcheck disable=SC2016
    equivalence ssh "$host" "cd '$REMOTE_DIR/current' &&" \
        'MICROPYPATH=".frozen:$PWD/src:$PWD:$PWD/../lib"' \
        ../bin/micropython -X heapsize=$MP_HEAP >"$tmp/remote.txt"
    echo "--- local"
    cat "$tmp/local.txt"
    echo "--- $host"
    cat "$tmp/remote.txt"
    if ! [ -s "$tmp/local.txt" ] || ! cmp -s "$tmp/local.txt" "$tmp/remote.txt"; then
        echo "the remote interpreter does not compute what the local one does: not started" >&2
        exit 1
    fi

    if [ -z "$names" ]; then
        spec="none planned"
    elif [ "$(state "$REMOTE_DIR/spec")" = done ]; then
        spec="ended there already ($REMOTE_DIR/spec/status), not started again"
    else
        # shellcheck disable=SC2086
        REMOTE_DIR=$REMOTE_DIR/spec JOBS=$SPEC_JOBS sh spec/remote.sh start "$host" $names \
            >"$tmp/spec.txt" 2>&1
        status=$?
        grep -v '^fetch it:' "$tmp/spec.txt" | sed 's/^/spec: /'
        [ $status -eq 0 ] || exit $status
        spec="started ($names; SPEC_JOBS=$SPEC_JOBS)"
    fi
    if [ -s "$tmp/pool.txt" ]; then
        ssh "$host" "cd '$REMOTE_DIR' && rm -f status pid run.log errors.log || exit
            nohup nice -n 10 sh -c 'xargs -P $jobs -L 1 sh run1.sh <pool.txt; echo \$? >status' \
                >run.log 2>&1 </dev/null & echo \$! >pid" || exit
    else
        ssh "$host" "cd '$REMOTE_DIR' && rm -f pid errors.log && : >run.log && echo 0 >status" ||
            exit
    fi
    echo "started on $host:$REMOTE_DIR: $(wc -l <"$tmp/pool.txt") pool jobs" \
        "($(grep -vc '^spec ' "$tmp/jobs.txt") per label), $jobs at a time on $vcpus vCPUs" \
        "under nice; spec: $spec"
    echo "follow it:  ssh $host tail -f $REMOTE_DIR/run.log"
    echo "fetch it:   make campaign-fetch CAMPAIGN_HOST=$host"
}

# Record a verdict: verdict NAME STATUS NOTE (the worst status kept).
verdict() {
    s=$2
    [ "$s" -le 2 ] 2>/dev/null || s=2
    [ "$s" -le "$worst" ] || worst=$s
    printf '  %-28s %s  %s\n' "$1" "$s" "$3" >>"$FETCH/verdicts.txt"
}

fetch() {
    pool=$(state "$REMOTE_DIR")
    if [ "$pool" = none ]; then
        echo "no campaign on $host:$REMOTE_DIR" >&2
        exit 2
    fi
    spec=absent
    if ssh "$host" "grep -q '^spec ' '$REMOTE_DIR/jobs.txt'"; then
        spec=$(state "$REMOTE_DIR/spec")
    fi
    if [ "$pool" = running ] || [ "$spec" = running ]; then
        echo "pool: $pool, $(progress)"
        [ "$pool" != running ] || ssh "$host" "tail -3 '$REMOTE_DIR/run.log'"
        case "$spec" in
        running)
            REMOTE_DIR=$REMOTE_DIR/spec FETCH=$FETCH/spec sh spec/remote.sh fetch "$host" |
                sed 's/^/spec: /'
            ;;
        absent) echo "spec: none planned" ;;
        *) echo "spec: $spec" ;;
        esac
        exit 0
    fi
    if [ "$pool" = stopped ]; then
        echo "the pool on $host:$REMOTE_DIR stopped before its end: start again to resume it" >&2
        exit 2
    fi
    counts=$(progress) || exit 2
    mkdir -p "$FETCH" || exit 2
    (cd "$FETCH" && rm -rf errors.log status verdicts.txt spec.txt times.txt bounds-*.txt \
        compare-*.txt records-*.tar.xz baseline spec) || exit 2
    rsync -a --delete --exclude '*.tmp' "$host:$REMOTE_DIR/out/" "$FETCH/out/" &&
        rsync -a "$host:$REMOTE_DIR/run.log" "$host:$REMOTE_DIR/status" \
            "$host:$REMOTE_DIR/plan.txt" "$host:$REMOTE_DIR/jobs.txt" \
            "$host:$REMOTE_DIR/pool.txt" "$host:$REMOTE_DIR/revs.txt" \
            "$host:$REMOTE_DIR/shifts.txt" "$host:$REMOTE_DIR/machine.txt" "$FETCH/" || exit
    ssh "$host" "cat '$REMOTE_DIR/errors.log' 2>/dev/null" >"$FETCH/errors.log"
    [ -s "$FETCH/errors.log" ] || rm -f "$FETCH/errors.log"
    labels=$(awk '{ print $1 }' "$FETCH/revs.txt")
    worst=0
    echo "verdicts in $FETCH (0 passed; 1 a breach, a difference or an unexpected TLC" \
        "outcome; 2 incomplete or an input problem):" >"$FETCH/verdicts.txt"

    echo "fetched into $FETCH: $counts (remote pool status $(cat "$FETCH/status"))"
    set -- $counts
    if [ -f "$FETCH/errors.log" ]; then
        echo "failed jobs (timeout or crash, no record; start again to retry them):"
        cat "$FETCH/errors.log"
    fi
    if [ "$1" -lt "$3" ]; then
        verdict pool 2 "$counts: jobs without record"
    else
        verdict pool "$(cat "$FETCH/status")" "$counts"
    fi

    if [ "$spec" != absent ]; then
        if [ "$spec" = done ]; then
            REMOTE_DIR=$REMOTE_DIR/spec FETCH=$FETCH/spec sh spec/remote.sh fetch "$host" \
                >"$FETCH/spec.txt" 2>&1
            status=$?
        else
            echo "the spec job did not end there ($spec): start again to run it" >"$FETCH/spec.txt"
            status=2
        fi
        sed 's/^/spec: /' "$FETCH/spec.txt"
        verdict spec $status spec.txt
    fi

    if [ -s "$FETCH/pool.txt" ]; then
        read -r vcpus jobs <"$FETCH/machine.txt"
        share=$(awk -v v="$vcpus" -v j="$jobs" 'BEGIN { s = v / j; print s < 1 ? s : 1 }')
        mp . tools/campaign.py times "$FETCH/out" --share "$share" --label current \
            --times "$TIMES" >"$FETCH/times.txt"
        verdict times $? "times.txt (share $share)"
        cat "$FETCH/times.txt"
    fi

    rev=$(awk '$1 == "current" { print $2 }' "$FETCH/revs.txt")
    archive=records-${rev:-unknown}.tar.xz
    set -- out plan.txt jobs.txt revs.txt
    [ ! -f "$FETCH/spec/run.log" ] || set -- "$@" spec/run.log
    tar -C "$FETCH" -cJf "$FETCH/$archive" "$@" || exit 2
    echo "packed: $FETCH/$archive ($(du -k "$FETCH/$archive" | cut -f1) kB)"

    if [ -s "$FETCH/pool.txt" ]; then
        expect=$(cat "$FETCH/shifts.txt")
        for label in $labels; do
            set -- --label "$label"
            [ -z "$expect" ] || set -- "$@" --expect "$expect"
            echo "--- bounds, label $label"
            mp . tools/campaign.py bounds "$FETCH/out" "$@" >"$FETCH/bounds-$label.txt"
            verdict "bounds $label" $? "bounds-$label.txt"
            cat "$FETCH/bounds-$label.txt"
        done
    fi

    if [ -n "$BASELINE" ] && [ -s "$FETCH/pool.txt" ]; then
        case "$BASELINE" in
        *.tar.xz | *.txz)
            mkdir -p "$FETCH/baseline" && tar -C "$FETCH/baseline" -xJf "$BASELINE" || exit 2
            base=$FETCH/baseline
            ;;
        *) base=$BASELINE ;;
        esac
        for label in $labels; do
            echo "--- compare, $BASELINE_LABEL of $BASELINE against $label"
            # shellcheck disable=SC2086
            mp . tools/campaign.py compare "$base" "$FETCH/out" \
                --label "$BASELINE_LABEL:$label" $COMPARE >"$FETCH/compare-$label.txt"
            verdict "compare $label" $? "compare-$label.txt"
            cat "$FETCH/compare-$label.txt"
        done
    fi

    echo "exit status: $worst" >>"$FETCH/verdicts.txt"
    echo "---"
    cat "$FETCH/verdicts.txt"
    exit "$worst"
}

case "$cmd" in
start) start "$@" ;;
fetch) fetch ;;
*) usage ;;
esac
