#!/usr/bin/env bash
# install.sh — set up graph-claude so every workflow run opens the graph.
#
#   ./install.sh            install
#   ./install.sh --uninstall remove the hook, leave the files
#   ./install.sh --status    show what is currently installed
#
# What it does: registers a PreToolUse hook on the Workflow tool in
# ~/.claude/settings.json pointing at this clone. Existing settings and hooks are
# preserved; the file is backed up first and validated after.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
HOOK="$DIR/wfviz-term"
MODE="${1:-install}"

b()  { printf '\033[1m%s\033[0m\n' "$1"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
no() { printf '  \033[31m✗\033[0m %s\n' "$1"; }
hm() { printf '  \033[33m!\033[0m %s\n' "$1"; }

python_json() { python3 -c "$1" "${@:2}"; }

# ── prerequisites ─────────────────────────────────────────────────────────
b "Checking prerequisites"
FAIL=0
if command -v python3 >/dev/null; then
  ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)"
else
  no "python3 is required"; FAIL=1
fi
[ -d "$HOME/.claude" ] && ok "Claude Code config at ~/.claude" || { no "~/.claude not found — is Claude Code installed?"; FAIL=1; }
if command -v tmux >/dev/null && command -v ttyd >/dev/null; then
  ok "tmux + ttyd (terminal panel available)"
else
  hm "tmux and/or ttyd missing — the graph works, the terminal panel does not"
  hm "  optional:  brew install tmux ttyd"
fi
[ "$FAIL" = 1 ] && { echo; no "cannot continue"; exit 1; }
chmod +x "$DIR/wfviz" "$DIR/wfviz-term" "$DIR/ccm" "$DIR/wfviz.py" "$DIR/gen_replay.py" 2>/dev/null

# ── settings.json surgery, non-destructive ────────────────────────────────
apply() {
  python3 - "$SETTINGS" "$HOOK" "$1" <<'PY'
import json, os, shutil, sys, time
path, hook, mode = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(path), exist_ok=True)
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        print(f"UNREADABLE {e}"); sys.exit(2)
    shutil.copy(path, f"{path}.bak.{int(time.time())}")

cmd = f"{hook} >/dev/null 2>&1"
pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])

def is_ours(entry):
    return any("wfviz-term" in (h.get("command") or "") for h in entry.get("hooks", []))

pre[:] = [e for e in pre if not (isinstance(e, dict) and is_ours(e))]   # drop any prior install
if mode == "install":
    pre.append({"matcher": "Workflow",
                "hooks": [{"type": "command", "command": cmd}]})
if not pre:
    data["hooks"].pop("PreToolUse", None)
if not data.get("hooks"):
    data.pop("hooks", None)

with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
json.load(open(path))            # validate what we just wrote
print("OK")
PY
}

case "$MODE" in
  --status|status)
    b "Status"
    if [ -f "$SETTINGS" ] && grep -q "wfviz-term" "$SETTINGS" 2>/dev/null; then
      ok "hook installed → $(grep -o '[^"]*wfviz-term' "$SETTINGS" | head -1)"
    else
      hm "hook not installed"
    fi
    curl -s -o /dev/null "http://127.0.0.1:${WFVIZ_PORT:-8777}/runs" \
      && ok "server running on :${WFVIZ_PORT:-8777}" || hm "server not running (start with ./wfviz)"
    exit 0;;
  --uninstall|uninstall)
    b "Removing the hook"
    R="$(apply uninstall)"
    [ "$R" = "OK" ] && ok "removed from $SETTINGS (backup kept)" || { no "$R"; exit 1; }
    echo; echo "  Files are untouched. Delete this directory to remove them."
    exit 0;;
esac

b "Installing"
R="$(apply install)"
if [ "$R" = "OK" ]; then
  ok "hook registered in $SETTINGS"
  ok "points at $HOOK"
else
  no "$R"; exit 1
fi

echo
b "Done. One thing left:"
cat <<EOF

  Claude Code loads hooks when a session starts, so the hook does not apply to
  a session that is already open. Restart Claude Code (or open a new session)
  and the graph will open by itself on every workflow run.

  Right now, without restarting:
      $DIR/wfviz            open the graph
      $DIR/wfviz-term       open it with the terminal panel

  To mirror a Claude session in that panel, start sessions with:
      $DIR/ccm

  Check or undo:
      ./install.sh --status
      ./install.sh --uninstall
EOF
