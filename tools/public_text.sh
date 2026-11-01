#!/bin/sh
# Fail when a tracked text file carries references to the private project
# logbook: session numbers (S10, "Session 35"), milestone or task ids
# (M3-12), owner attributions or logbook file names. The repository is
# public; the history of a choice is told by its content.
#
#   tools/public_text.sh    check the tracked and untracked (not ignored)
#                           files
#
# Exit status 1 and one line per match if any is found.

set -u
cd "$(dirname "$0")/.." || exit 2

pattern='(^|[^-A-Za-z0-9_])S[1-9][0-9]([^0-9A-Za-z_]|$)'
pattern="$pattern"'|[Ss]ession [0-9]'
pattern="$pattern"'|(^|[^A-Za-z0-9])M[1-6]-[0-9]'
pattern="$pattern"'|(^|[^A-Za-z])[Oo]wner([^A-Za-z]|$)'
pattern="$pattern"'|(JOURNAL|DEFECTS|DESIGN_PROVENANCE)\.md'

if git grep --untracked -n -I -E "$pattern" -- . ':!tools/public_text.sh'; then
    echo "public_text: logbook references above (see the header of $0)" >&2
    exit 1
fi
