---
name: breakout
description: Toggle BREAK-IN in your terminal (free-play) — alias of /breakin
---

Run this exactly, then report the one-line result to the user:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/hooks/breakout-hook.sh" toggle breakout
```

This is the back-compat alias for `/breakin`, kept so muscle memory from earlier versions still works. It opens BREAK-IN — the endless ascent — in a tmux pane split in the current window (free-play, so it ignores Claude's thinking state), or closes it if it's already open. It requires the session to be running inside tmux. If it isn't, tell the user the game needs a tmux session and that the auto-launch-while-thinking still works without the manual toggle.
