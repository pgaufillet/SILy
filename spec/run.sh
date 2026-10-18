#!/bin/sh
# Run the TLC models of spec/ and check each outcome against its expectation.
#
#   spec/run.sh all       every configuration of cfg/
#   spec/run.sh pass      the configurations that must hold
#   spec/run.sh defects   the configurations that must violate a named
#                         invariant or property (defects of the current
#                         design, limits of the choices, non-vacuity witnesses)
#   spec/run.sh NAME...   the named configurations (cfg/NAME.cfg)
#
# Each cfg/<Module>_<case>.cfg checks <Module>.tla. Its second line states
# the expected outcome: "\* expect: holds" or "\* expect: violated <Name>".
#
# Environment: TLA_JAR (tla2tools.jar), JAVA, WORKERS, OUT (TLC logs and
# counterexample traces, one file per configuration). Exit status 1 if any
# outcome differs from its expectation.

set -u
cd "$(dirname "$0")" || exit 2

TLA_JAR=${TLA_JAR:-$HOME/.local/lib/tla2tools.jar}
JAVA=${JAVA:-java}
WORKERS=${WORKERS:-auto}
OUT=${OUT:-${TMPDIR:-/tmp}/sily-spec}

expectation() {
    sed -n '2s/^\\\* expect: //p' "cfg/$1.cfg"
}

case "${1:-}" in
"")
    echo "usage: $0 all|pass|defects|NAME..." >&2
    exit 2
    ;;
all | pass | defects)
    mode=$1
    names=""
    for f in cfg/*.cfg; do
        name=$(basename "$f" .cfg)
        case "$mode:$(expectation "$name")" in
        all:* | pass:holds | defects:violated*) names="$names $name" ;;
        esac
    done
    ;;
*) names="$*" ;;
esac

if [ ! -f "$TLA_JAR" ]; then
    echo "tla2tools.jar not found: set TLA_JAR" >&2
    exit 2
fi
mkdir -p "$OUT"

bad=0
for name in $names; do
    module=${name%%_*}
    expect=$(expectation "$name")
    log="$OUT/$name.log"
    rm -rf "$OUT/meta/$name"
    start=$(date +%s)
    "$JAVA" -XX:+UseParallelGC -cp "$TLA_JAR" tlc2.TLC -workers "$WORKERS" \
        -noGenerateSpecTE -difftrace -metadir "$OUT/meta/$name" \
        -config "cfg/$name.cfg" "$module.tla" >"$log" 2>&1
    secs=$(($(date +%s) - start))
    states=$(grep -E 'distinct states found' "$log" | tail -1 |
        sed -E 's/.* ([0-9,]+) distinct states found.*/\1/')
    case "$expect" in
    holds) grep -q 'No error has been found' "$log" ;;
    "violated "*) grep -qE "(Invariant|property) ${expect#violated } (is|was) violated" "$log" ;;
    *) false ;;
    esac
    if [ $? -eq 0 ]; then verdict=ok; else verdict=UNEXPECTED bad=1; fi
    printf '%-11s %-32s %-24s %9s states %4ss  %s\n' \
        "$verdict" "$name" "${expect:-no expectation}" "${states:-?}" "$secs" "$log"
done
exit $bad
