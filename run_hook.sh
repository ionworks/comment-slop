#!/bin/sh
# PostToolUse wrapper for comment_slop.py.
#
# Exit 2 is the protocol's "block and show this to the agent", which the
# detector uses for findings.

# Every interpreter also exits 2 for its own failures: `uv` on an unwritable
# cache, `python3` on a missing script, argparse on a bad flag.

# Passing those through would report a tooling error as findings, blocking the
# edit loop.

# So findings need both: exit 2, and output that opens with `comment-slop:`.
# Anything else exits 0, text still on stderr.

# The payload is read once and replayed, so the python3 fallback runs even
# when uv has consumed stdin.
set -u

# From $0, not the project dir: the detector ships beside this wrapper, and
# `packages/comment-slop/` is one repo's layout, not the package's.
self=$0
# A symlinked registration is followed to the real file, which `src/` sits
# beside. Plain `readlink`, because older macOS has no `readlink -f`.
while [ -L "$self" ]; do
    link=$(readlink "$self")
    case "$link" in
        /*) self=$link ;;
        *) self="$(dirname "$self")/$link" ;;
    esac
done
script="$(dirname "$self")/src/comment_slop.py"
root="${CLAUDE_PROJECT_DIR:-.}"
payload=$(cat)
out=""

# Not a clean edit: without the detector nothing was checked. Exit 1 is the
# protocol's non-blocking error, so the edit goes through and the user is told.
if [ ! -r "$script" ]; then
    echo "comment-slop wrapper: no detector at $script" >&2
    exit 1
fi

# Pass the repo's own pin: uv resolves .python-version from a cwd the hook does
# not control. Absent or unsatisfiable, the python3 fallback below runs.
uv_python=""
version=""
[ -r "$root/.python-version" ] &&
    version=$(tr -d '[:space:]' <"$root/.python-version")
[ -n "$version" ] && uv_python="--python=$version"

run() {
    printf '%s' "$payload" | "$@" "$script" --hook 2>&1
}

# The one place that decides what findings look like. `warn` prints the same
# prefix on a clean run, so the prefix alone is not enough.
is_findings() {
    [ "$status" -eq 2 ] || return 1
    case "$out" in
        "comment-slop: "*) return 0 ;;
    esac
    return 1
}

status=1
# --no-python-downloads: uv would otherwise fetch an interpreter to satisfy the pin.
if command -v uv >/dev/null 2>&1; then
    out=$(run uv run --no-project --no-python-downloads $uv_python python)
    status=$?
fi
# uv is absent or produced something that is not findings. A clean exit means a
# clean file; anything else is uv's own failure, so fall back to python3.
if ! is_findings && [ "$status" -ne 0 ] && command -v python3 >/dev/null 2>&1
then
    out=$(run python3)
    status=$?
fi

[ -n "$out" ] && printf '%s\n' "$out" >&2
is_findings && exit 2
exit 0
