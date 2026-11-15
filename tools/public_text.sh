#!/bin/sh
# Fail when a tracked text file carries references to the private project
# logbook: session numbers (S10, "Session 35"), milestone or task ids
# (M3-12), defect ids (D1 to D99), owner attributions or logbook file
# names. The repository is public; the history of a choice is told by its
# content.
#
#   tools/public_text.sh    check the tracked and untracked (not ignored)
#                           files
#   tools/public_text.sh --commits [RANGE]
#                           check the commits of RANGE (default: the ones
#                           not pushed, @{upstream}..HEAD) before a push:
#                           the lines they add and their messages, which
#                           also carry no tool attribution, no
#                           conventional prefix ("fix: ") and a title of
#                           at most 50 characters
#
# Exit status 1 and one line per match if any is found.

set -u
cd "$(dirname "$0")/.." || exit 2

pattern='(^|[^-A-Za-z0-9_])S[1-9][0-9]([^0-9A-Za-z_]|$)'
pattern="$pattern"'|[Ss]ession [0-9]'
pattern="$pattern"'|(^|[^A-Za-z0-9])M[1-6]-[0-9]'
pattern="$pattern"'|(^|[^A-Za-z0-9_])D[1-9][0-9]?([^0-9A-Za-z_]|$)'
pattern="$pattern"'|(^|[^A-Za-z])[Oo]wner([^A-Za-z]|$)'
pattern="$pattern"'|(JOURNAL|DEFECTS|DESIGN_PROVENANCE)\.md'

if [ "${1:-}" != --commits ]; then
    if git grep --untracked -n -I -E "$pattern" -- . ':!tools/public_text.sh'; then
        echo "public_text: logbook references above (see the header of $0)" >&2
        exit 1
    fi
    exit 0
fi

range=${2:-@\{upstream\}..HEAD}
commits=$(git rev-list --reverse "$range") || exit 2
tmp=$(mktemp) || exit 2
trap 'rm -f "$tmp"' EXIT
found=0
report() {  # report COMMIT WHAT: print each line of $tmp, prefixed
    while IFS= read -r line; do
        echo "$1 $2: $line"
        found=1
    done < "$tmp"
}
for c in $commits; do
    id=$(git rev-parse --short "$c")
    title=$(git log -1 --format=%s "$c")
    git log -1 --format=%B "$c" | grep -E "$pattern" > "$tmp"
    report "$id" message
    git log -1 --format=%B "$c" \
        | grep -i -E 'co-authored-by|generated with|anthropic\.com' > "$tmp"
    report "$id" attribution
    printf '%s\n' "$title" | grep -E '^[A-Za-z]+(\([^)]*\))?!?: ' > "$tmp"
    report "$id" prefix
    if [ "$(printf '%s' "$title" | wc -m)" -gt 50 ]; then
        printf '%s\n' "$title" > "$tmp"
        report "$id" "title over 50 characters"
    fi
    git show --format= --no-color -U0 "$c" -- . ':!tools/public_text.sh' \
        | grep '^+' | grep -v '^+++' | grep -E "$pattern" > "$tmp"
    report "$id" "added line"
done
if [ "$found" = 1 ]; then
    echo "public_text: outgoing commits above (see the header of $0)" >&2
    exit 1
fi
