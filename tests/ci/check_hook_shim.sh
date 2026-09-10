#!/bin/sh
# Exercise tools/blink-hook.sh under one specific shell.
#
#   tests/ci/check_hook_shim.sh [dash|busybox|bash|sh]
#
# Same reasoning as check_shim.sh, and higher stakes. This runs on EVERY tool
# call -- many times a minute during real work -- under whatever /bin/sh the
# customer's machine provides, and Claude Code runs it inline, so a stray byte
# on stdout or stderr lands in the user's own session.
#
# It also now takes a session id out of the payload and uses it as a FILENAME,
# which makes this the only place in the product where attacker-shaped input
# reaches a path. The traversal and injection cases below are the point of
# this file, not extras.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

WHICH="${1:-sh}"
ci_label "$WHICH"
SHIM_SRC="$ROOT/tools/blink-hook.sh"
WORK="${TMPDIR:-/tmp}/blink-hook-$WHICH"

case "$WHICH" in
dash) SH="dash" ;;
busybox) SH="busybox sh" ;;
bash) SH="bash" ;;
sh) SH="sh" ;;
*) echo "unknown shell: $WHICH" >&2; exit 1 ;;
esac
command -v "${SH%% *}" >/dev/null 2>&1 || { echo "$SH not installed" >&2; exit 1; }

rm -rf "$WORK"
HOME="$WORK/home"
mkdir -p "$HOME"
export HOME

SHIM="$HOME/blink-hook.sh"
cp "$SHIM_SRC" "$SHIM"
DIR="$HOME/.blink/state"

# A realistic payload, carrying several fields we must be seen NOT to keep.
PAYLOAD='{"session_id":"abc-123","transcript_path":"/x/secret.jsonl","cwd":"/home/secret/proj","tool_name":"Bash","hook_event_name":"PreToolUse","last_assistant_message":"the secret is swordfish"}'

printf '== hook shim under %s\n' "$SH"

# 1. Records the event under the session's own file, silently.
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" PreToolUse 2>"$WORK/err.txt")
[ -z "$out" ] || fail "printed to stdout: [$out]"
[ -s "$WORK/err.txt" ] && fail "wrote to stderr: $(cat "$WORK/err.txt")"
grep -q '"event":"PreToolUse"' "$DIR/abc-123.state" ||
	fail "event not recorded: $(cat "$DIR/abc-123.state" 2>/dev/null)"
ok "records the event under its own session file, silently"

# 2. A bare 10-digit epoch. The provider divides nothing and compares against
#    now(); a quoted or empty value parses as malformed and the pip never lights.
grep -qE '"t":[0-9]{10}' "$DIR/abc-123.state" ||
	fail "timestamp is not a bare epoch: $(cat "$DIR/abc-123.state")"
ok "timestamp is a bare epoch integer"

# 3. Nothing but the event, the session id and the clock. The shim reads
#    session_id and agent_id and NOTHING else from the payload, so none of
#    these can appear anywhere under the state directory.
for secret in secret.jsonl "home/secret" Bash swordfish; do
	if grep -rq "$secret" "$DIR" 2>/dev/null; then
		fail "payload content leaked into the state dir: $secret"
	fi
done
ok "keeps nothing from the payload but the ids"

# 4. Atomic write.
# The shim writes "$sid.state.$$.tmp", so a test for "$sid.state.tmp" could
# never match and never fail. Match the pid form instead. A leftover temp is
# not cosmetic: nothing sweeps these -- ClaudeStateProvider.scan() ignores
# anything not ending in .state, and SessionEnd removes only $sid.state and
# $sid/ -- so one per killed hook accumulates for the life of the install.
if ls "$DIR"/abc-123.state.*.tmp >/dev/null 2>&1; then
	fail "atomic write left a temp file behind"
fi
ok "atomic write leaves no temp file"

# 5. Two sessions do not overwrite each other. This is the whole reason the
#    single global slot was replaced.
printf '{"session_id":"sess-two"}' | $SH "$SHIM" Stop >/dev/null 2>&1
grep -q '"event":"PreToolUse"' "$DIR/abc-123.state" ||
	fail "a second session clobbered the first"
grep -q '"event":"Stop"' "$DIR/sess-two.state" || fail "second session not recorded"
ok "two sessions keep separate files"

# 6. Agents: one file each, and a stop removes exactly its own.
printf '{"session_id":"abc-123","agent_id":"agent-one"}' | $SH "$SHIM" SubagentStart >/dev/null 2>&1
printf '{"session_id":"abc-123","agent_id":"agent-two"}' | $SH "$SHIM" SubagentStart >/dev/null 2>&1
[ -f "$DIR/abc-123/agent-one" ] && [ -f "$DIR/abc-123/agent-two" ] ||
	fail "agent files not created"
printf '{"session_id":"abc-123","agent_id":"agent-one"}' | $SH "$SHIM" SubagentStop >/dev/null 2>&1
[ ! -f "$DIR/abc-123/agent-one" ] || fail "SubagentStop did not remove its agent"
[ -f "$DIR/abc-123/agent-two" ] || fail "SubagentStop removed the WRONG agent"
ok "one file per agent; a stop removes exactly its own"

# 7. SessionEnd takes the whole session with it, agents included.
printf '{"session_id":"abc-123"}' | $SH "$SHIM" SessionEnd >/dev/null 2>&1
[ ! -e "$DIR/abc-123.state" ] || fail "SessionEnd left the state file"
[ ! -e "$DIR/abc-123" ] || fail "SessionEnd left the agent directory"
[ -f "$DIR/sess-two.state" ] || fail "SessionEnd removed an unrelated session"
ok "SessionEnd removes its own session and only its own"

# 8. PATH TRAVERSAL. The session id becomes a filename, so this is the one
#    place attacker-shaped input reaches a path. The character class in the
#    extraction pattern IS the sanitiser -- a value with a slash simply fails
#    to match and falls through to "unknown".
#    Two levels up from $DIR is $HOME, so this is the file a successful
#    traversal would create. The old canary appended an absolute path to
#    four `../` and could never have matched anything.
printf '{"session_id":"../../pwned"}' |
	$SH "$SHIM" PreToolUse >/dev/null 2>&1
[ ! -e "$HOME/pwned.state" ] || fail "PATH TRAVERSAL: wrote outside the state dir"
[ -f "$DIR/unknown.state" ] || fail "traversal attempt did not fall back to 'unknown'"
ok "a traversing session id cannot escape the state directory"

# 8b. The bare names `.` and `..` -- no slash, so the old class admitted them,
#     and `$DIR/..` is ~/.blink itself. With an agent id naming a file there,
#     SubagentStart truncated it and SubagentStop deleted it. The signing keys
#     live in that directory.
printf 'keep me' > "$HOME/.blink/precious"
for bad in '.' '..'; do
	printf '{"session_id":"%s","agent_id":"precious"}' "$bad" |
		$SH "$SHIM" SubagentStart >/dev/null 2>&1
	printf '{"session_id":"%s","agent_id":"precious"}' "$bad" |
		$SH "$SHIM" SubagentStop >/dev/null 2>&1
	printf '{"session_id":"%s"}' "$bad" | $SH "$SHIM" SessionEnd >/dev/null 2>&1
done
[ "$(cat "$HOME/.blink/precious")" = "keep me" ] ||
	fail "a dot session id reached a file outside the state dir"
[ -d "$DIR" ] || fail "a dot session id removed the state directory"
# ...and the same via the AGENT id, which reaches an rm -f of its own.
printf '{"session_id":"abc-123","agent_id":".."}' |
	$SH "$SHIM" SubagentStop >/dev/null 2>&1
printf '{"session_id":"abc-123","agent_id":"../precious"}' |
	$SH "$SHIM" SubagentStop >/dev/null 2>&1
[ "$(cat "$HOME/.blink/precious")" = "keep me" ] ||
	fail "a traversing agent id reached a file outside the session dir"
ok "dot and dot-dot ids cannot reach ~/.blink"

# 8c. The top-level session id wins over one inside a tool's arguments.
#     PreToolUse payloads carry tool_input verbatim, and tools have their own
#     session_id fields; the greedy match used to take the LAST one.
printf '{"session_id":"outer-1","tool_input":{"session_id":"inner-9"}}' |
	$SH "$SHIM" PreToolUse >/dev/null 2>&1
[ -f "$DIR/outer-1.state" ] || fail "did not take the top-level session id"
[ ! -e "$DIR/inner-9.state" ] || fail "took a session id out of tool_input"
ok "the top-level session id is used, not one inside tool arguments"

# 8d. Private to the user: the state files name the sessions someone has open.
#     find's -perm with octal bits is portable across GNU and BSD; `ls -l`
#     column parsing is not.
printf '{"session_id":"priv-1"}' | $SH "$SHIM" PreToolUse >/dev/null 2>&1
for p in "$DIR" "$DIR/priv-1.state"; do
	[ -e "$p" ] || fail "expected $p to exist"
	[ -z "$(find "$p" -prune \( -perm -040 -o -perm -004 \) -print)" ] ||
		fail "readable by others: $(ls -ld "$p")"
done
ok "state files are readable by this user alone"

# 8e. The project directory name. It comes out of the same payload by the same
#     means as the session id, so every shape the id extractor had to survive
#     is repeated here -- a tool argument carrying its own copy of the key, the
#     traversal names, and the characters that would end the value early. This
#     one lands inside a JSON string rather than a filename, so the quote that
#     would close that string early is a refusal too.
check_name() {
	rm -f "$DIR/abc.state"
	printf '%s' "$1" | $SH "$SHIM" PreToolUse >/dev/null 2>&1
	[ -f "$DIR/abc.state" ] || fail "wrote no state file at all for [$1]"
	got=$(sed -n 's/.*"name":"\([^"]*\)".*/\1/p' "$DIR/abc.state")
	[ "$got" = "$2" ] || fail "name from [$1] was [$got], wanted [$2]"
}
#     check_no_name insists the state file EXISTS before checking that the key
#     is absent. Reading it with `sed ... 2>/dev/null` cannot tell "no name
#     key" from "no file at all", so a refusal case asserted that way would go
#     on passing if the shim crashed and wrote nothing -- the sanitiser's own
#     tests are the last place that can afford to pass vacuously.
check_no_name() {
	rm -f "$DIR/abc.state"
	printf '%s' "$1" | $SH "$SHIM" PreToolUse >/dev/null 2>&1
	[ -f "$DIR/abc.state" ] || fail "wrote no state file at all for [$1]"
	if grep -q '"name"' "$DIR/abc.state"; then
		fail "wrote a name key for [$1]: $(cat "$DIR/abc.state")"
	fi
}

# The ordinary case: the last segment only, never the path above it.
check_name '{"session_id":"abc","cwd":"/Users/kfir/Projects/LiveClaudeUi"}' \
	'LiveClaudeUi'

# Windows, where the separator is an escaped backslash in the JSON.
check_name '{"session_id":"abc","cwd":"C:\\\\Users\\\\kfir\\\\Blink"}' 'Blink'

# A tool argument carrying its own cwd must not win. The top-level key is
# first, which is the same rule _ident relies on for session_id.
check_name '{"session_id":"abc","cwd":"/home/k/Real","tool_input":{"cwd":"/tmp/Fake"}}' \
	'Real'

# ...and with NO top-level cwd, "first occurrence" is no longer the same thing
# as "top-level", so first-occurrence alone promoted the tool's own argument to
# the name on the display. Nothing is the only right answer here: the header
# promises the tool arguments are not read, and unknown is already what an
# absent key means. Nested one deep, two deep, and through an array.
check_no_name '{"session_id":"abc","tool_input":{"cwd":"/tmp/Fake"}}'
check_no_name '{"session_id":"abc","tool_input":{"deep":{"cwd":"/tmp/Fake"}}}'
check_no_name '{"session_id":"abc","tool_input":[{"cwd":"/tmp/Fake"}]}'

# A top-level cwd standing after a top-level array still reads: the guard
# counts the `{` that entering an object requires, not brackets.
check_name '{"session_id":"abc","edits":["x"],"cwd":"/home/k/Real"}' 'Real'

# Characters that would break out of the JSON string are refused whole.
check_no_name '{"session_id":"abc","cwd":"/tmp/bad\"name"}'
check_no_name '{"session_id":"abc","cwd":"/tmp/has space"}'

# A backslash INSIDE the name is the one that cannot be refused, because on
# the wire it is byte-for-byte the Windows separator two cases up: both are
# `X\\Y` and nothing short of a JSON decoder can tell a separator from an
# escaped literal. So it is treated as a separator and the tail is taken. That
# mislabels a unix directory genuinely called `bad\name` -- a wrong label, not
# an unsafe one, because the backslash is consumed by the pattern and what
# gets written still carries neither `\` nor `"`. Refusing it instead would
# refuse every Windows path there is.
check_name '{"session_id":"abc","cwd":"/tmp/bad\\\\name"}' 'name'
# A bracket expression, not '\\' or '\': POSIX strips the backslash of any
# special meaning inside brackets, so this matches one literal backslash -- and
# it does so without putting a backslash next to a closing quote, which is what
# SC1003 flags as a mis-quoted apostrophe, whatever grep would do with it.
# (Do not open that line with the tool's own name -- it parses as a directive.)
grep -q '[\]' "$DIR/abc.state" && fail "a backslash reached the state file"

# Relative traversal names never become a label.
check_no_name '{"session_id":"abc","cwd":"/tmp/.."}'

# Over-long is refused rather than truncated mid-name.
check_no_name "{\"session_id\":\"abc\",\"cwd\":\"/tmp/$(printf 'a%.0s' $(seq 1 40))\"}"

# No cwd at all is normal: the key is omitted, not written empty.
check_no_name '{"session_id":"abc"}'
ok "captures the project directory name, and only the final segment"

# 8f. The PARENT pid. This is what lets the daemon tell a session that died
#     without firing SessionEnd from one that is merely quiet, and the whole
#     feature rests on the recorded number outliving the hook -- so that is
#     what is asserted, rather than a value.
#
#     $$ instead of $PPID would look perfectly reasonable in the source and
#     record the hook's OWN pid, which is dead by the time anything reads the
#     file. `kill -0` on it here is the cheapest way to catch that: the process
#     that ran this hook is this script's shell, and it is still alive.
rm -f "$DIR/pidcheck.state"
printf '{"session_id":"pidcheck"}' | $SH "$SHIM" PreToolUse >/dev/null 2>&1
grep -qE '"pid":[1-9][0-9]*[,}]' "$DIR/pidcheck.state" ||
	fail "pid is not a bare positive integer: $(cat "$DIR/pidcheck.state")"
got_pid=$(sed -n 's/.*"pid":\([0-9]*\).*/\1/p' "$DIR/pidcheck.state")
kill -0 "$got_pid" 2>/dev/null ||
	fail "recorded pid $got_pid is already gone -- it is not the parent"
ok "records the parent pid, and it outlives the hook"

# 9. Quote and command injection in the id.
rm -f "$DIR/unknown.state"
printf '{"session_id":"a\\"; touch %s/owned; echo \\"b"}' "$WORK" |
	$SH "$SHIM" PreToolUse >/dev/null 2>&1
[ ! -e "$WORK/owned" ] || fail "COMMAND INJECTION via session_id"
ok "a quoting session id cannot run a command"

# 10. Exit status is always 0 -- a non-zero exit is a signal to Claude Code.
printf '%s' "$PAYLOAD" | $SH "$SHIM" Stop >/dev/null 2>&1 ||
	fail "non-zero exit on a normal run"
ok "exits 0"

# 11. No argument at all. Claude Code always passes one, but a hand-edited
#     settings file may not.
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" 2>"$WORK/err11.txt")
[ -z "$out" ] || fail "printed something with no argument: [$out]"
[ -s "$WORK/err11.txt" ] && fail "no-argument run wrote to stderr"
grep -q '"event":"unknown"' "$DIR/abc-123.state" ||
	fail "no-argument run did not record 'unknown'"
ok "a missing event name records 'unknown' rather than failing"

# 12. An unwritable HOME breaks capture silently. Our own capture is allowed
#     to be broken; it must never print on a path that runs many times a minute.
RO="$WORK/readonly"
mkdir -p "$RO"
chmod 500 "$RO"
out=$(HOME="$RO" sh -c "printf '%s' '$PAYLOAD' | $SH '$SHIM' PreToolUse" 2>"$WORK/err12.txt" || true)
[ -z "$out" ] || fail "printed with an unwritable HOME: [$out]"
[ -s "$WORK/err12.txt" ] && fail "unwritable HOME wrote to stderr: $(cat "$WORK/err12.txt")"
chmod 700 "$RO"
ok "an unwritable HOME breaks capture silently"

# 13. Stdin is drained -- a hook that exits without reading SIGPIPEs its writer.
big=$(awk 'BEGIN{printf "{\"session_id\":\"big\",\"pad\":\""; for(i=0;i<20000;i++) printf "x"; printf "\"}"}')
out=$(printf '%s' "$big" | $SH "$SHIM" PostToolUse 2>"$WORK/err13.txt")
[ -z "$out" ] || fail "printed on a large payload"
[ -s "$WORK/err13.txt" ] && fail "large payload wrote to stderr: $(cat "$WORK/err13.txt")"
ok "drains a large payload without complaint"

# 14. The Codex state directory. The same shim serves Codex's hooks, which use
#     the same event names and the same stdin fields; the second argument is
#     the only thing that differs, and it must move every write.
CODEXDIR="$HOME/.blink/state-codex"
out=$(printf '%s' "$PAYLOAD" | $SH "$SHIM" PreToolUse codex 2>"$WORK/err14.txt")
[ -z "$out" ] || fail "codex run printed to stdout: [$out]"
[ -s "$WORK/err14.txt" ] && fail "codex run wrote to stderr: $(cat "$WORK/err14.txt")"
grep -q '"event":"PreToolUse"' "$CODEXDIR/abc-123.state" ||
	fail "codex event not recorded under state-codex"
ok "the codex argument writes under ~/.blink/state-codex"

# 14b. ...and never into the Claude directory. A Codex session counted as a
#      Claude one is the entire reason the second directory exists: the pip
#      row would attribute it to the wrong tool and the wrong account.
printf '{"session_id":"codex-only"}' | $SH "$SHIM" Stop codex >/dev/null 2>&1
[ -f "$CODEXDIR/codex-only.state" ] || fail "codex session not recorded"
[ ! -e "$DIR/codex-only.state" ] ||
	fail "a codex session leaked into the Claude directory"
printf '{"session_id":"claude-only"}' | $SH "$SHIM" Stop >/dev/null 2>&1
[ ! -e "$CODEXDIR/claude-only.state" ] ||
	fail "a Claude session leaked into the codex directory"
ok "the two state directories never cross"

# 14c. An unrecognised second argument falls back to the Claude directory
#      rather than becoming a path fragment. This value is written by our own
#      installer, but it lands in a config file a person can hand-edit, and
#      nothing hand-edited gets to choose a directory under $HOME.
printf '{"session_id":"weird"}' | $SH "$SHIM" Stop '../../../etc' >/dev/null 2>&1
[ -f "$DIR/weird.state" ] ||
	fail "an unknown tool argument did not fall back to state/"
# A glob rather than `ls | grep`: shellcheck refuses the latter (SC2010), and
# it is right to -- a filename with a newline in it would be counted twice.
# This is a directory whose contents an attacker-shaped session id helps
# choose, which is the whole point of the assertion below.
extra=
for p in "$HOME"/.blink/*; do
	[ -e "$p" ] || continue		# no matches: the glob stays literal
	case "${p##*/}" in
	state|state-codex|precious) ;;
	*) extra="${extra:+$extra }${p##*/}" ;;
	esac
done
[ -z "$extra" ] ||
	fail "an unknown tool argument created something in ~/.blink: $extra"
ok "an unknown tool argument cannot choose a directory"

# 14d. Every sanitiser case the Claude directory has, repeated for the codex
#      one. The sanitisers are shared code, but the directory they write into
#      is not, and this is the file that has to prove the second one is as
#      safe as the first.
printf 'keep me' > "$HOME/.blink/precious"
printf '{"session_id":"../../pwned"}' |
	$SH "$SHIM" PreToolUse codex >/dev/null 2>&1
[ ! -e "$HOME/pwned.state" ] ||
	fail "codex PATH TRAVERSAL: wrote outside the state dir"
[ -f "$CODEXDIR/unknown.state" ] ||
	fail "codex traversal did not fall back to 'unknown'"
for bad in '.' '..'; do
	printf '{"session_id":"%s","agent_id":"precious"}' "$bad" |
		$SH "$SHIM" SubagentStart codex >/dev/null 2>&1
	printf '{"session_id":"%s","agent_id":"precious"}' "$bad" |
		$SH "$SHIM" SubagentStop codex >/dev/null 2>&1
	printf '{"session_id":"%s"}' "$bad" |
		$SH "$SHIM" SessionEnd codex >/dev/null 2>&1
done
[ "$(cat "$HOME/.blink/precious")" = "keep me" ] ||
	fail "a dot session id reached ~/.blink through the codex argument"
[ -d "$CODEXDIR" ] || fail "a dot session id removed the codex state directory"
[ -z "$(find "$CODEXDIR" -prune \( -perm -040 -o -perm -004 \) -print)" ] ||
	fail "codex state directory readable by others: $(ls -ld "$CODEXDIR")"
ok "the codex directory is as sanitised and as private as the Claude one"

# 15. THE WAITING MARKER, and the race it exists to escape.
#
#     Codex fires PreToolUse and PermissionRequest in the SAME SECOND -- twice,
#     in the one interactive session anyone has captured. Those are two copies
#     of this script running at once, and the slot below them is written with
#     mv -f, so whichever renames last wins regardless of which event happened
#     first. Lose that race and the panel says a session is working while it is
#     blocked on a person, for as long as the person takes to answer.
#
#     The marker is a different file, so there is no race left to lose. That is
#     what the two orders below check: not "did the race happen to go our way
#     this time" but "both orders give the same answer", which is exhaustive
#     rather than sampled.
W='{"session_id":"wait-1","cwd":"/home/secret/proj"}'
printf '%s' "$W" | $SH "$SHIM" PermissionRequest >/dev/null 2>&1
[ -f "$DIR/wait-1.waiting" ] || fail "PermissionRequest left no waiting marker"
[ ! -s "$DIR/wait-1.waiting" ] ||
	fail "the marker has contents: $(cat "$DIR/wait-1.waiting")"
ok "PermissionRequest raises a waiting marker, and it is empty"

# 15b. The LOSING order, written down: PreToolUse arrives second and clobbers
#      the slot. The marker must survive it, which is why PreToolUse is not in
#      the removal list -- it is the one event known to land in the same second.
printf '%s' "$W" | $SH "$SHIM" PreToolUse >/dev/null 2>&1
grep -q '"event":"PreToolUse"' "$DIR/wait-1.state" ||
	fail "the second event did not reach the slot at all"
[ -f "$DIR/wait-1.waiting" ] ||
	fail "PreToolUse cleared the marker: the race is back"
ok "a same-second PreToolUse takes the slot but not the marker"

# 15c. The other order, for completeness: the marker is raised after the slot
#      already says running, and the answer is the same.
printf '{"session_id":"wait-2"}' | $SH "$SHIM" PreToolUse >/dev/null 2>&1
printf '{"session_id":"wait-2"}' | $SH "$SHIM" PermissionRequest >/dev/null 2>&1
[ -f "$DIR/wait-2.waiting" ] || fail "no marker when PermissionRequest is second"
ok "both same-second orders end with the marker present"

# 15d. Every way a person can answer clears it. A waiting state with no way out
#      is worse than no waiting state at all: the panel would call for someone's
#      attention until the abandon sweep an hour later.
for clear in UserPromptSubmit PostToolUse Interrupt Stop; do
	printf '%s' "$W" | $SH "$SHIM" PermissionRequest >/dev/null 2>&1
	[ -f "$DIR/wait-1.waiting" ] || fail "marker not re-raised before $clear"
	printf '%s' "$W" | $SH "$SHIM" "$clear" >/dev/null 2>&1
	[ ! -e "$DIR/wait-1.waiting" ] || fail "$clear did not clear the marker"
done
ok "every event that means a person answered clears the marker"

# 15e. SessionEnd takes it too. Nothing else ever removes a marker whose
#      session is gone except the daemon's hour-long sweep.
printf '%s' "$W" | $SH "$SHIM" PermissionRequest >/dev/null 2>&1
printf '{"session_id":"wait-1"}' | $SH "$SHIM" SessionEnd >/dev/null 2>&1
[ ! -e "$DIR/wait-1.waiting" ] || fail "SessionEnd left the waiting marker"
[ -f "$DIR/wait-2.waiting" ] || fail "SessionEnd removed another session's marker"
ok "SessionEnd removes its own marker and only its own"

# 15f. Private, like the slot beside it: the marker's mere existence says
#      somebody has an approval prompt open on this machine.
[ -z "$(find "$DIR/wait-2.waiting" \( -perm -040 -o -perm -004 \) -print)" ] ||
	fail "the marker is readable by others: $(ls -l "$DIR/wait-2.waiting")"
ok "the waiting marker is readable by this user alone"

# 15g. And an unwritable directory costs the marker and NOTHING else. This
#      line wants to be `:`, which is a POSIX SPECIAL built-in -- and a
#      redirection error on a special built-in makes the shell EXIT, skipping
#      the `exit 0` at the bottom of the shim and handing Claude Code a failing
#      hook. Measured: status 1 under sh, 2 under dash.
chmod 500 "$RO"
out=$(HOME="$RO" sh -c "printf '%s' '$W' | $SH '$SHIM' PermissionRequest; printf 'status=%s' \$?" 2>"$WORK/err15.txt")
chmod 700 "$RO"
[ "$out" = "status=0" ] || fail "PermissionRequest into an unwritable HOME: [$out]"
[ -s "$WORK/err15.txt" ] && fail "unwritable marker wrote to stderr: $(cat "$WORK/err15.txt")"
ok "an unwritable marker path still exits 0, silently"

# 15h. The SAME defect, in the SubagentStart branch, which is SHIPPED code and
#      predates the marker entirely. An agent slot that cannot be written must
#      cost the count and nothing else -- never the exit status, because a
#      failing hook is a failing tool call for the person using it.
chmod 500 "$RO"
out=$(HOME="$RO" sh -c "printf '%s' '$W' | $SH '$SHIM' SubagentStart; printf 'status=%s' \$?" 2>"$WORK/err15h.txt")
chmod 700 "$RO"
[ "$out" = "status=0" ] || fail "SubagentStart into an unwritable HOME: [$out]"
[ -s "$WORK/err15h.txt" ] && fail "unwritable agent slot wrote to stderr: $(cat "$WORK/err15h.txt")"
ok "an unwritable agent slot still exits 0, silently"


# 16. THE TOMBSTONE, and the census it exists to correct.
#
#     Removing the slot is all a slot-counting source ever needed. Codex's
#     rollout reader is not one: a closed session leaves a file that simply
#     stops growing, which is also what a long quiet turn looks like, so it
#     kept counting the session for the full hour the file took to age out --
#     and the union that lets a session with no slot count at all put back
#     every session this branch had just removed. Measured on the owner's
#     desk: one live slot, two Claude sessions, four recent rollouts, and a
#     panel reading six.
#
#     Absence cannot carry the news, because absence is also what a session
#     that never had a hook looks like, and the union exists for that one. So
#     SessionEnd leaves a file that says so.
printf '{"session_id":"tomb-1","cwd":"/home/secret/proj"}' | $SH "$SHIM" PreToolUse >/dev/null 2>&1
printf '{"session_id":"tomb-2"}' | $SH "$SHIM" PreToolUse >/dev/null 2>&1
printf '{"session_id":"tomb-1"}' | $SH "$SHIM" SessionEnd >/dev/null 2>&1
[ -f "$DIR/tomb-1.ended" ] || fail "SessionEnd left no tombstone"
[ ! -s "$DIR/tomb-1.ended" ] ||
	fail "the tombstone has contents: $(cat "$DIR/tomb-1.ended")"
[ ! -e "$DIR/tomb-2.ended" ] || fail "SessionEnd buried a second session"
[ -f "$DIR/tomb-2.state" ] || fail "SessionEnd removed an unrelated slot"
ok "SessionEnd leaves an empty tombstone, for its own session alone"

# 16b. And any later event takes it away again. `claude --resume` and `codex
#      resume` reopen a session under the SAME id, so a tombstone that outlived
#      the ending would suppress a session somebody is sitting in front of --
#      the resurrection bug inverted, and just as visible on the panel.
printf '{"session_id":"tomb-1"}' | $SH "$SHIM" SessionStart >/dev/null 2>&1
[ ! -e "$DIR/tomb-1.ended" ] || fail "a resumed session kept its tombstone"
[ -f "$DIR/tomb-1.state" ] || fail "the resumed session wrote no slot"
ok "a session that speaks again loses its tombstone"

# 16c. Private, like the slot and the marker: the names of the sessions
#      somebody has been running are nobody else's business.
printf '{"session_id":"tomb-2"}' | $SH "$SHIM" SessionEnd >/dev/null 2>&1
[ -z "$(find "$DIR/tomb-2.ended" \( -perm -040 -o -perm -004 \) -print)" ] ||
	fail "the tombstone is readable by others: $(ls -l "$DIR/tomb-2.ended")"
ok "the tombstone is readable by this user alone"

# 16d. The codex argument gets its own, in its own directory. The daemon reads
#      the two directories as two different accounts, so a tombstone landing in
#      the wrong one would silence a Claude session on a Codex SessionEnd.
printf '{"session_id":"cx-tomb"}' | $SH "$SHIM" PreToolUse codex >/dev/null 2>&1
printf '{"session_id":"cx-tomb"}' | $SH "$SHIM" SessionEnd codex >/dev/null 2>&1
[ -f "$CODEXDIR/cx-tomb.ended" ] || fail "no tombstone in the codex directory"
[ ! -e "$DIR/cx-tomb.ended" ] ||
	fail "the codex tombstone landed in the Claude directory"
ok "the codex directory gets its own tombstone and only its own"

# 16e. And an unwritable directory costs the tombstone and NOTHING else. Same
#      defect as 15g, in a branch that had no redirection in it until today:
#      `:` is a POSIX SPECIAL built-in, so a redirection error on it EXITS the
#      shell, skipping the exit 0 at the bottom of the shim and handing Claude
#      Code a failing hook -- on the event that fires as a person closes their
#      terminal. And 2>/dev/null must come BEFORE the '>' target, because the
#      complaint about a failed redirect is printed by the shell rather than by
#      the command.
chmod 500 "$RO"
out=$(HOME="$RO" sh -c "printf '{\"session_id\":\"tomb-9\"}' | $SH '$SHIM' SessionEnd; printf 'status=%s' \$?" 2>"$WORK/err16.txt")
chmod 700 "$RO"
[ "$out" = "status=0" ] || fail "SessionEnd into an unwritable HOME: [$out]"
[ -s "$WORK/err16.txt" ] && fail "unwritable tombstone wrote to stderr: $(cat "$WORK/err16.txt")"
ok "an unwritable tombstone path still exits 0, silently"

printf 'PASS [%s]\n' "$WHICH"
