# 747 Terminal Games

[![CI](https://github.com/The747Lab/747-terminal-games/actions/workflows/ci.yml/badge.svg)](https://github.com/The747Lab/747-terminal-games/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)](LICENSE) ![No dependencies](https://img.shields.io/badge/dependencies-none-black)

**Play a game in your terminal while your AI codes.**

When Claude Code is thinking, a game appears in a pane beside your work. It **pauses the instant Claude replies** so you never miss the answer, and **resumes when you send your next prompt** — same run, same score. Close it any time. Zero setup beyond installing the plugin, and zero dependencies: Python 3 and `curses`, nothing else.

![First-run intro — a textmode fly-through](assets/747-intro.gif)

## The four titles

The first time a game opens, you pick one. All four are finished games with a HUD, a
scoring table, a win (or a depth) screen and a personal-best file — not demos.

---

### BREAK-IN — *endless ascent*

You never clear the wall. You crack a hatch in the **ceiling**, thread the ball back up
through the hole, and climb into the chamber above. Then you do it again. Forever.
`C 1` is the only number that matters, and it only ever goes up.

```
▌ BREAK-IN  ·     160 ♥♥    C 1    BREACH ▰▱▱▱▱
                        ······
 ▔▔▛▜▛▜▛▜▔▔▔▔▔▔▔▔▔▔▔▛▜▛▜▛▜    ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▛▜▛▜▛▜▔▔▔▔▔▔▔▔▔▔▔▛▜▛▜▛▜▔▔
 ▄▄▄▄▄ █████ █████ █████       █████ ▄▄▄▄▄ █████ ▄▄▄▄▄ █████ █████ █████ ▄▄▄▄▄
 ▄▄▄▄▄ ▄▄▄▄▄ ▄▄▄▄▄ █████       █████ ▄▄▄▄▄ █████ ▄▄▄▄▄ ▄▄▄▄▄ ▄▄▄▄▄ █████ ▄▄▄▄▄
 ▄▄▄▄▄ ▄▄▄▄▄ █████ ▄▄▄▄▄       █████ █████ █████ ▄▄▄▄▄ ▄▄▄▄▄ █████ ▄▄▄▄▄ ▄▄▄▄▄
 ▄▄▄▄▄ ▄▄▄▄▄ █████ ▄▄▄▄▄             ▄▄▄▄▄ █████ ▄▄▄▄▄ ▄▄▄▄▄ █████ ▄▄▄▄▄ ▄▄▄▄▄

                                       ●
                       ▀▀▀▀▀▀▀▀
 CHAMBER 1 · BEST 1                                                THE 747 LAB
```

The cyan `▛▜` panels in the ceiling are the way out, and `BREACH ▰▱▱▱▱` says how close
you are. Take a chamber without losing a life and your streak multiplies; every seventh
chamber is a vault.

---

### SKYRUN — *POV space run*

A **seven-sector delivery run**, seen from the windshield of a car flying through
interstellar space. Shoot the alien craft, dodge the rock, thread the gate at the end of
every sector. Clear sector 7 and the run is complete — then keep flying in OVERRUN if you want.

```
▌ SKYRUN    ·      40 ▰▰▱   S 1/7  ▸▹▹▹▹▹▹▹  13%  ×1





                             ·          ☩       ·
                                    ☩◄◄‹◦›►►☩           ··
                         ▏                    ·        ▕      ·
                         ▏                     ·       ▕       ·
                         ▏                             ▕
                         ▏              ▲              ▕
                          ◉███████████████████████████◉
 ←→ move · [space] shoot ☩ · dodge █ · grab ◈                       THE 747 LAB
```

`S 1/7` and the distance bar are on screen from the first frame, so you always know where
the run ends. Three shields, no lives — the only way out is to run out of hull.

---

### JETWASH — *side-on sky runner*

The same fiction, the opposite camera. One button up, one button down: **jump** the solid
block, **slam** through the brittle dither. Seven gates, **7,470 metres**, one number on the HUD.
Thrust *is* speed, so collecting it makes the run both faster and shorter.

```
▌ JETWASH   ·     741 ▣▣▣▢  G1     ▸▸▸▸▸▸▸▹▹▹  ▮▮▮▯▯▯▯▯▯▯
                       ·        ·   ·     -
                                 ·      ·                 ·        ·
                    ▓▓▓▓                ▓▓▓▓▓
▓▓▓            ▓▓▓  ▓▓▓▓                ▓▓▓▓▓▓▓▓  ▓▓▓▓▓               ▓▓▓▓
════════════════════════════════════════════════════════════════════════════════
  ▛▜           ◈  ◈                 ║║      ▛▜
                                    ║║
            ◈        ◈              ║║
          ►                         ║║      ◈                              ▟▙
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱
╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱  ╱
 ↑ jump · ↓ slam · █ solid · ▒ breaks                              THE 747 LAB
```

Every hazard is telegraphed more than a second before it reaches you, so a hit is always
a decision you got wrong — never a surprise.

---

### ASTROS — *invaders*

Seven waves, then the big one. The top row is worth triple, so greed is a real choice.
Chain your kills inside 1.2 seconds to build a multiplier; bunkers arrive in wave 3,
divers in wave 5.

```
▌ ASTROS    ·       0 ▲▲    W1/7   ▰▰▰▰▰▰▰▰▰▰

                                     ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼
                                     ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼
                                     ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼
                                     ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼
                                     ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼  ▼▼

                                     !

                     ← → move   [space] fire   dodge the !
                                        ▲
 WAVE 1 · BEST 0                                                   THE 747 LAB
```

The opening seconds teach the game by layout rather than by a tutorial box: one bomb
falls, alone, into empty space, so you learn what `!` means before it can cost you.

## Install (Claude Code plugin)

```
/plugin marketplace add The747Lab/747-terminal-games
/plugin install 747-terminal-games@747-terminal-games
```

That's it. The next time Claude thinks, you'll be asked once whether you want to play —
`y` to try it, `a` to auto-open every time, `o` to never ask. Say yes and the picker
comes up: choose a title with `1`–`4` or the arrow keys. Your choice is remembered.

## Play

**In any game** — `q` closes it. The game pauses itself when Claude replies; `space` (or
`p` in SKYRUN) keeps it running anyway.

| | Controls |
|---|---|
| **BREAK-IN** | `←` `→` (or `a` / `d`), or the mouse — move the paddle. `space` — keep playing while Claude is idle. |
| **SKYRUN** | `←` `→` `↑` `↓` (or `w` `a` `s` `d`), or the mouse — steer. `space` — fire. `v` — chase camera (needs 13+ rows). `p` — keep flying. |
| **JETWASH** | `↑` / `space` / `w` — jump. `↓` / `s` — slam. Two buttons, no mouse. |
| **ASTROS** | `←` `→` (or `a` / `d`), or the mouse — move. `space` — fire. |

**Slash commands** open a free-play round any time, ignoring Claude's state:
`/breakin` · `/skyrun` · `/jetwash` · `/astros` — and `/breakout` still works as an alias
for `/breakin`.

## Requirements

- **tmux** (recommended) — the game opens as a split pane right in your window while Claude thinks, and **disappears the moment Claude replies** — your terminal goes back to exactly how it was, no tab to close. Your run is kept alive in the background and rejoins, mid-game, on your next prompt.
- **iTerm2 without tmux** — the game splits your current iTerm window natively (same in-place feel).
- **Elsewhere** — auto-open needs tmux. The games still run standalone any time: `python3 games/jetwash.py --free`.
- **Python 3.9+** with `curses` (standard on macOS and Linux). No pip install, ever.

Every title stays readable down to an 80×8 pane, falls back to 16 colours, and falls back
to pure ASCII on a terminal that can't do UTF-8.

## Modes

A tiny file at `~/.747-terminal-games/mode` controls auto-launch:

| Value  | Behavior                                        |
|--------|-------------------------------------------------|
| `ask`  | Ask once per session (default)                  |
| `auto` | Always open while Claude thinks                 |
| `off`  | Never auto-open (the slash commands still work) |

## Which game auto-opens

Whichever you last chose in the picker. It is stored as one word in
`~/.747-terminal-games/game`, so you can also set it directly:

```
echo jetwash > ~/.747-terminal-games/game
```

The slash commands ignore this file — they always open the title you named.

## About

Built by [The 747 Lab](https://github.com/The747Lab). A growing line of terminal games.

There is a `747` hidden in every one of them. It is never on the label — it is in the
game. Find it; it does something.

MIT licensed. PRs and new games welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
