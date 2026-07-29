---
name: breakin
description: Toggle the BREAK-IN game pane in your terminal (free-play)
---

Run this exactly, then report the one-line result to the user:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/hooks/breakout-hook.sh" toggle breakout
```

This opens BREAK-IN — the endless ascent: crack the ceiling, thread the ball up through the hole, and climb into the chamber above, forever — in a tmux pane split in the current window (free-play, so it ignores Claude's thinking state), or closes it if it's already open. It requires the session to be running inside tmux. If it isn't, tell the user the game needs a tmux session and that the auto-launch-while-thinking still works without the manual toggle.

The title key stays `breakout` on purpose: it is the file name and the pane title, and renaming it would orphan any game already running in the background.
