#!/bin/sh
# Run the TLC models of spec/ and check each outcome against its expectation.
#
#   spec/run.sh all|full  every configuration of cfg/
#   spec/run.sh quick     the configurations whose recorded duration in
#                         durations.txt is at most QUICK_SECS seconds
#   spec/run.sh pass      the configurations that must hold
#   spec/run.sh defects   the configurations that must violate a named
#                         invariant or property (defects of the current
#                         design, limits of the choices, non-vacuity witnesses)
#   spec/run.sh NAME...   the named configurations (cfg/NAME.cfg)
#   spec/run.sh record DIR  merge the durations recorded in DIR (the
#                         durations.d directory of OUT) into durations.txt
#
# Each cfg/<Module>_<case>.cfg checks <Module>.tla. Its second line states
# the expected outcome: "\* expect: holds" or "\* expect: violated <Name>".
#
# Environment:
#   TLA_JAR     tla2tools.jar
#   JAVA        the java command
#   JOBS        configurations run at a time (default 1)
#   WORKERS     TLC workers per configuration (default auto with JOBS = 1,
#               else the processors divided by JOBS, at least 1)
#   HEAP        Java heap per configuration, -Xmx (default the JVM's with
#               JOBS = 1, else three quarters of the memory divided by JOBS)
#   MAXSECS     time limit per configuration in seconds: a configuration
#               still running is stopped and reported TIMEOUT
#   QUICK_SECS  bound of the quick tier (default 60)
#   MACHINE     label of the machine in durations.txt (default
#               <processors>cpu-<memory>G, -j<JOBS> appended when
#               JOBS > 1)
#   DURATIONS   the durations file (default durations.txt of spec/)
#   OUT         TLC logs and counterexample traces, one file per
#               configuration, and durations.d/ (the durations of the run)
#
# Configurations without a recorded duration run first (they may be long),
# then the others, longest first. A result line is printed when its
# configuration ends; the run's durations are merged into durations.txt at
# the end. Exit status 1 if any outcome differs from its expectation or a
# configuration timed out.

set -u
cd "$(dirname "$0")" || exit 2

TLA_JAR=${TLA_JAR:-$HOME/.local/lib/tla2tools.jar}
JAVA=${JAVA:-java}
JOBS=${JOBS:-1}
QUICK_SECS=${QUICK_SECS:-60}
MAXSECS=${MAXSECS:-}
DURATIONS=${DURATIONS:-$PWD/durations.txt}
OUT=${OUT:-${TMPDIR:-/tmp}/sily-spec}

cpus=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)
mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)
mem_kb=${mem_kb:-0}
if [ -z "${MACHINE:-}" ]; then
    MACHINE=${cpus}cpu-$(((mem_kb + 524288) / 1048576))G
    # Durations under JOBS > 1 are longer (shared processors): the label says so.
    if [ "$JOBS" -gt 1 ]; then
        MACHINE=$MACHINE-j$JOBS
    fi
fi
if [ "$JOBS" -gt 1 ]; then
    WORKERS=${WORKERS:-$((cpus / JOBS > 1 ? cpus / JOBS : 1))}
    if [ -z "${HEAP:-}" ] && [ "$mem_kb" -gt 0 ]; then
        HEAP=$((mem_kb / 1024 * 3 / 4 / JOBS))m
    fi
fi
WORKERS=${WORKERS:-auto}
HEAP=${HEAP:-}
export TLA_JAR JAVA WORKERS HEAP MAXSECS MACHINE DURATIONS OUT

expectation() {
    sed -n '2s/^\\\* expect: //p' "cfg/$1.cfg"
}

# Run one configuration: print its result line, write its duration line
# into OUT/durations.d. Exit status 1 unless the outcome is the expected one.
run_one() {
    name=$1
    if [ ! -f "cfg/$name.cfg" ]; then
        printf '%-11s %-32s no such configuration\n' UNEXPECTED "$name"
        return 1
    fi
    module=${name%%_*}
    expect=$(expectation "$name")
    log="$OUT/$name.log"
    rm -rf "$OUT/meta/$name"
    set -- "$JAVA" -XX:+UseParallelGC
    if [ -n "$HEAP" ]; then set -- "$@" "-Xmx$HEAP"; fi
    set -- "$@" -cp "$TLA_JAR" tlc2.TLC -workers "$WORKERS" \
        -noGenerateSpecTE -difftrace -metadir "$OUT/meta/$name" \
        -config "cfg/$name.cfg" "$module.tla"
    if [ -n "$MAXSECS" ]; then set -- timeout -k 10 "$MAXSECS" "$@"; fi
    start=$(date +%s)
    "$@" >"$log" 2>&1
    status=$?
    secs=$(($(date +%s) - start))
    states=$(sed -nE 's/.* ([0-9,]+) distinct states found.*/\1/p' "$log" | tail -1)
    record=$secs
    if [ -n "$MAXSECS" ] && { [ $status -eq 124 ] || [ $status -eq 137 ]; }; then
        verdict=TIMEOUT record=">$MAXSECS"
    else
        case "$expect" in
        holds) grep -q 'No error has been found' "$log" ;;
        "violated "*) grep -qE "(Invariant|property) ${expect#violated } (is|was) violated" "$log" ;;
        *) false ;;
        esac
        if [ $? -eq 0 ]; then verdict=ok; else verdict=UNEXPECTED; fi
    fi
    printf '%s %s %s\n' "$name" "$record" "$MACHINE" >"$OUT/durations.d/$name"
    printf '%-11s %-32s %-24s %9s states %4ss  %s\n' \
        "$verdict" "$name" "${expect:-no expectation}" "${states:-?}" "$secs" "$log"
    [ "$verdict" = ok ]
}

# Merge the duration lines of the files of $1 into DURATIONS, one line per
# configuration (the newest kept), sorted by name, the header comment kept.
record() {
    for f in "$1"/*; do
        [ -f "$f" ] || return 0
        break
    done
    tmp="$DURATIONS.tmp.$$"
    {
        if [ -f "$DURATIONS" ]; then
            grep '^#' "$DURATIONS"
        else
            echo "# Durations of the TLC configurations, written by run.sh."
        fi
        { cat "$1"/*; [ ! -f "$DURATIONS" ] || grep -v '^#' "$DURATIONS"; } |
            awk 'NF >= 2 && !seen[$1]++' | LC_ALL=C sort -k1,1
    } >"$tmp" && mv "$tmp" "$DURATIONS"
}

# Read names on stdin; print "run KEY NAME" for those to run (KEY orders
# them, longest first) and "unrecorded NAME" for those the quick tier
# leaves out for want of a recorded duration.
schedule() {
    awk -v f="$DURATIONS" -v quick="$1" -v bound="$QUICK_SECS" '
    BEGIN {
        while ((getline line < f) > 0) {
            n = split(line, a, " ")
            if (n >= 2 && a[1] !~ /^#/) d[a[1]] = a[2]
        }
    }
    NF == 0 { next }
    {
        if (!($1 in d)) {
            if (quick) print "unrecorded", $1
            else print "run", 3000000000, $1
        } else if (d[$1] ~ /^>/) {
            if (!quick) print "run", 2000000000 + substr(d[$1], 2), $1
        } else if (!quick || d[$1] + 0 <= bound + 0) {
            print "run", d[$1] + 0, $1
        }
    }'
}

case "${1:-}" in
"")
    echo "usage: $0 all|full|quick|pass|defects|NAME...|record DIR" >&2
    exit 2
    ;;
--one)
    run_one "$2"
    exit
    ;;
record)
    record "${2:?record DIR}"
    exit
    ;;
all | full | quick | pass | defects)
    mode=$1
    names=""
    for f in cfg/*.cfg; do
        name=$(basename "$f" .cfg)
        case "$mode:$(expectation "$name")" in
        all:* | full:* | quick:* | pass:holds | defects:violated*) names="$names $name" ;;
        esac
    done
    quick=0
    [ "$mode" = quick ] && quick=1
    plan=$(printf '%s\n' $names | schedule $quick)
    if [ $quick -eq 1 ]; then
        echo "quick: $(printf '%s\n' "$plan" | grep -c '^run ') configurations" \
            "recorded at most $QUICK_SECS s; $(printf '%s\n' "$plan" |
                grep -c '^unrecorded ') without a recorded duration left" \
            "out (full tier only)"
    fi
    names=$(printf '%s\n' "$plan" | awk '$1 == "run" {print $2, $3}' |
        sort -k1,1nr | cut -d' ' -f2)
    ;;
*) names="$*" ;;
esac

if [ ! -f "$TLA_JAR" ]; then
    echo "tla2tools.jar not found: set TLA_JAR" >&2
    exit 2
fi
[ -n "$names" ] || exit 0
rm -rf "$OUT/durations.d"
mkdir -p "$OUT/durations.d"

trap 'record "$OUT/durations.d"; exit 130' INT TERM
printf '%s\n' $names | xargs -P "$JOBS" -n 1 sh ./run.sh --one
bad=$?
record "$OUT/durations.d"
[ $bad -eq 0 ]
