#!/usr/bin/env python3
"""release-title.py — verify a finished title, then REGISTER it everywhere.

    python3 scripts/release-title.py <title-key> [--version X.Y.Z] [--check-only]

WHY THIS EXISTS. A title is finished the moment games/<key>.py plays well, and
then it is still UNREACHABLE: the picker will not offer it, no slash command
opens it, the hook refuses it as an unknown title, and CI never launches it.
JAYWALK sat finished and unreachable across four separate registration points.
Every future title hits the same four, so this is a script, not a checklist:

    1. games/breakout.py       CATALOGUE  — the picker's line-up
    2. commands/<key>.md                  — the slash command
    3. hooks/breakout-hook.sh  GAMES=     — the hook whitelist (a security gate:
                                            the title flows into a shell exec
                                            string and into file paths)
    4. .github/workflows/ci.yml           — the launch / soak / resize / picker /
                                            routing matrices

Everything it does is IDEMPOTENT: run it twice and the second run reports
"already registered" and changes nothing. It NEVER runs git — it stages
working-tree changes only, and the human commits.

Zero third-party imports and Python 3.9 syntax only, because that is the floor
the line itself ships against.
"""
from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_DIR = os.path.join(ROOT, "games")
PICKER = os.path.join(GAMES_DIR, "breakout.py")
HOOK = os.path.join(ROOT, "hooks", "breakout-hook.sh")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")
COMMANDS = os.path.join(ROOT, "commands")
PLUGIN_JSON = os.path.join(ROOT, ".claude-plugin", "plugin.json")
MARKET_JSON = os.path.join(ROOT, ".claude-plugin", "marketplace.json")
README = os.path.join(ROOT, "README.md")

PY = sys.executable or "python3"
SOCK = "rel747"          # our OWN tmux server: never touch the user's session
NUMWORD = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six",
           7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten"}

# The no-network gate, mirrored VERBATIM from ci.yml ("No network calls in
# shipped code"). Kept as one constant so a drift between the two is visible.
NET_RE = r"urllib|requests|socket|http://|https://[a-z]"

RESULTS = []             # [(phase, step, ok, evidence)]


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------
def record(phase, step, ok, evidence):
    RESULTS.append((phase, step, bool(ok), str(evidence).strip()))
    print("%s %-46s %s" % ("GREEN" if ok else "RED  ", step,
                           str(evidence).strip().replace("\n", " | ")[:400]))
    return bool(ok)


def run(cmd, **kw):
    """Returns (rc, stdout+stderr). Never raises on a non-zero exit."""
    kw.setdefault("cwd", ROOT)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True, **kw)
    out, _ = p.communicate()
    return p.returncode, out or ""


def py_snippet(src, env=None):
    """Run a snippet in a FRESH interpreter — importing a game module mutates
    curses and sys.path, so it can never share ours."""
    e = dict(os.environ)
    if env:
        e.update(env)
    return run([PY, "-c", src], env=e)


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def tail(text, n=6):
    lines = [l for l in text.splitlines() if l.strip()]
    return " | ".join(lines[-n:]) if lines else "(no output)"


# ---------------------------------------------------------------------------
# title metadata, derived from the game itself
# ---------------------------------------------------------------------------
def tagline(key):
    """Line 3 of the module docstring is the one-sentence pitch, by convention
    across every shipped title."""
    src = read(os.path.join(GAMES_DIR, key + ".py"))
    m = re.match(r'\s*(?:#![^\n]*\n)?"""(.*?)"""', src, re.S)
    if not m:
        return ""
    lines = [l.strip() for l in m.group(1).splitlines()]
    body = [l for l in lines[1:] if l]
    return body[0] if body else ""


def blurb_for(key, budget=54):     # 54 == the longest blurb already shipping
    #                                (skyrun's) as rendered, so it is a size the
    #                                picker is already proven to lay out at 62x20.
    """A picker blurb, built from the tagline's clauses: the first clause, the
    separator, then as many following clauses as fit. Names verbs and shapes,
    never hues — the picker is the first thing a mono pane draws, and the CI hue
    gate fails the whole build on "shoot cyan". {sep} is a TOKEN, never a literal
    glyph: use_ascii() rebinds the separator well after import time, so a baked-in
    "·" would survive as the one non-ASCII byte on a pure-ASCII screen."""
    clauses = [c.strip() for c in re.split(r"[.;]", tagline(key)) if c.strip()]
    if not clauses:
        return "a 747 Lab title {sep} play it"
    def lower1(s):
        return s[0].lower() + s[1:] if s else s
    head = lower1(clauses[0])
    rest = []
    used = len(head) + 3
    for c in clauses[1:]:
        c = lower1(c)
        add = len(c) + (2 if rest else 0)
        if used + add > budget:
            break
        rest.append(c)
        used += add
    if not rest:
        return head + " {sep} " + key
    return head + " {sep} " + ", ".join(rest)


def catalogue_rows(src=None):
    src = src if src is not None else read(PICKER)
    block = re.search(r"CATALOGUE = \[(.*?)^\]", src, re.S | re.M)
    if not block:
        return []
    return re.findall(r'\(\s*"([a-z0-9_]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]*)"\s*\)',
                      block.group(1))


# ---------------------------------------------------------------------------
# 1. VERIFY
# ---------------------------------------------------------------------------
def v_compile(key):
    rc, out = run([PY, "-m", "py_compile", "games/%s.py" % key])
    return record("verify", "py_compile games/%s.py" % key, rc == 0,
                  "compiled clean on %s" % sys.version.split()[0] if rc == 0 else tail(out))


def v_no_network(key):
    """The EXACT grep from ci.yml's "No network calls in shipped code", scoped
    to the one file. Same -rnE, same alternation, same include filter."""
    rc, out = run(["grep", "-rnE", NET_RE, "games/%s.py" % key, "--include=*.py"])
    ok = rc != 0                        # grep found nothing == the gate passes
    return record("verify", "no-network gate (ci.yml pattern)", ok,
                  "grep -rnE %r found no match" % NET_RE if ok else tail(out))


def v_py39(key):
    """ci.yml "No 3.10+ syntax": AST scan for `match`, plus a real import, which
    is the only thing that catches an EVALUATED PEP-604 union."""
    src = '''
import ast, importlib, pathlib, sys
sys.path.insert(0, "games")
p = pathlib.Path("games/%s.py")
bad = []
for n in ast.walk(ast.parse(p.read_text())):
    if type(n).__name__ == "Match":
        bad.append("match statement (3.10+)")
try:
    importlib.import_module(p.stem)
    print("parses AND imports on " + sys.version.split()[0])
except SyntaxError as e:
    bad.append("SyntaxError: %%s" %% e)
except TypeError as e:
    bad.append("PEP-604 union evaluated at import: %%s" %% e)
for b in bad:
    print("FAIL", b)
sys.exit(1 if bad else 0)
''' % key
    rc, out = py_snippet(src)
    return record("verify", "3.9 floor: no match stmt, imports clean", rc == 0, tail(out, 2))


def v_mono_colour(key):
    """ci.yml "A terminal with no colour degrades, it never crashes": force the
    exact ValueError CPython 3.10+ raises when start_color() failed."""
    src = '''
import curses, importlib, sys, traceback
sys.path.insert(0, "games")
def boom(*a, **k):
    raise ValueError("Color pair is greater than COLOR_PAIRS-1 (-1).")
curses.init_pair = boom
curses.color_pair = boom
curses.start_color = boom
curses.use_default_colors = boom
for attr in ("COLORS", "COLOR_PAIRS"):
    if hasattr(curses, attr):
        delattr(curses, attr)
fail = []
m = importlib.import_module("%s")
try:
    p = m.Palette()
    print("Palette() built %%d roles with colour unavailable"
          %% len([k for k in vars(p) if not k.startswith("_")]))
except Exception as e:
    fail.append("Palette(): %%s: %%s" %% (type(e).__name__, e)); traceback.print_exc()
for helper in ("cpair", "ipair"):
    if not hasattr(m, helper):
        fail.append("missing helper " + helper); continue
    try:
        getattr(m, helper)(1, 2) if helper == "ipair" else getattr(m, helper)(1)
    except Exception as e:
        fail.append("%%s raised %%s" %% (helper, type(e).__name__))
for f in fail:
    print("FAIL", f)
sys.exit(1 if fail else 0)
''' % key
    rc, out = py_snippet(src)
    return record("verify", "colour-less degrade (ci.yml ValueError gate)", rc == 0,
                  tail(out, 2))


def v_hue_names(key):
    """ci.yml "On-screen text names shapes, never colours", AST-exact: real
    string literals only, docstrings excluded."""
    src = '''
import ast, re, sys
hues = re.compile(r"\\b(red|cyan|green|gold|magenta|violet|yellow|blue|white|grey|gray)\\b", re.I)
f = "games/%s.py"
tree = ast.parse(open(f).read())
docs = set()
for n in ast.walk(tree):
    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        d = ast.get_docstring(n, clean=False)
        if d is not None:
            docs.add(d)
fail = []
for n in ast.walk(tree):
    if not isinstance(n, ast.Constant) or not isinstance(n.value, str):
        continue
    s = n.value
    if s in docs or len(s) < 6 or " " not in s:
        continue
    if hues.search(s):
        fail.append("%%s:%%d names a colour on screen: %%r" %% (f, n.lineno, s))
for x in fail:
    print("FAIL", x)
if not fail:
    print("no on-screen string teaches the game by naming a hue")
sys.exit(1 if fail else 0)
''' % key
    rc, out = py_snippet(src)
    return record("verify", "hue-name AST gate (ci.yml)", rc == 0, tail(out, 3))


def v_glyph_families():
    """ci.yml "One glyph family per role". A LINE-WIDE invariant (the families
    live in skyrun), re-run here so a title cannot land while it is broken."""
    src = '''
import importlib, sys
sys.path.insert(0, "games")
sky = importlib.import_module("skyrun")
fail = []
for mode in ("utf8", "ascii"):
    if mode == "ascii":
        sky.use_ascii()
    hazard = set(sky.RAMP) | {sky.BLOCK, sky.MIDBLOCK}
    pickup = {sky.COIN_CH, sky.COIN_FAR, sky.POD_CH}
    target = {sky.ALIEN_CH, sky.ARTIFACT_CH}
    star = {sky.DOT}
    for a, an, b, bn in ((hazard, "HAZARD", pickup, "PICKUP"),
                         (hazard, "HAZARD", target, "TARGET"),
                         (pickup, "PICKUP", target, "TARGET"),
                         (star, "STAR", pickup, "PICKUP"),
                         (star, "STAR", target, "TARGET")):
        both = a & b
        if both:
            fail.append("%s: %s and %s share %r" % (mode, an, bn, sorted(both)))
if "RAMP[max(1," not in open("games/skyrun.py").read():
    fail.append("draw_rock no longer floors the shading ramp at index 1")
for f in fail:
    print("FAIL", f)
if not fail:
    print("HAZARD / PICKUP / TARGET / STAR stay disjoint in utf8 and ascii")
sys.exit(1 if fail else 0)
'''
    rc, out = py_snippet(src)
    return record("verify", "glyph-disjointness gate (ci.yml)", rc == 0, tail(out, 2))


# --- tmux harness -----------------------------------------------------------
def tmux(*args):
    return run(["tmux", "-L", SOCK] + list(args))


def tmux_alive(sess):
    rc, _ = tmux("has-session", "-t", sess)
    return rc == 0


def tmux_kill(sess):
    tmux("kill-session", "-t", sess)


def capture(sess):
    rc, out = tmux("capture-pane", "-p", "-t", sess)
    return out if rc == 0 else ""


def launch(key, cols, rows, sess, state_dir, err_path, extra_env=None, flags="--free"):
    env = "BREAKOUT747_STATE=%s" % state_dir
    if extra_env:
        env = env + " " + extra_env
    cmd = "env %s %s games/%s.py %s --session %s 2>%s" % (
        env, PY, key, flags, sess, err_path)
    tmux_kill(sess)
    return tmux("new-session", "-d", "-s", sess, "-x", str(cols), "-y", str(rows), cmd)


def v_launch(key, cols, rows, tag, extra_env=None, wait=3.0):
    tmp = tempfile.mkdtemp(prefix="rel747-")
    err = os.path.join(tmp, "err")
    sess = "v_%s_%s" % (key, tag)
    try:
        open(err, "w").close()
        launch(key, cols, rows, sess, tmp, err, extra_env)
        time.sleep(wait)
        alive = tmux_alive(sess)
        stderr = read(err) if os.path.exists(err) else ""
        ok = alive and not stderr.strip()
        why = ("alive after %.0fs at %dx%d, stderr empty" % (wait, cols, rows)) if ok else \
              ("died on launch" if not alive else "stderr: " + tail(stderr, 4))
        if not alive:
            why += " | stderr: " + tail(stderr, 6)
        return record("verify", "headless launch %dx%d%s" %
                      (cols, rows, "" if not extra_env else " [%s]" % tag), ok, why)
    finally:
        tmux_kill(sess)
        shutil.rmtree(tmp, ignore_errors=True)


def v_seamless(key):
    """THE CONTRACT THIS LINE SELLS: the game runs while Claude thinks, VANISHES
    into a frozen pane the moment Claude replies, resumes on the next prompt, and
    exits within a second of session end taking its state file with it. Driven by
    the state file, exactly as the hook drives it — no --free, so the game is
    genuinely obeying the state and not just playing regardless."""
    tmp = tempfile.mkdtemp(prefix="rel747-seam-")
    sess = "test-%s" % key
    err = os.path.join(tmp, "err")
    state = os.path.join(tmp, "state-%s" % sess)
    notes = []
    ok = True
    try:
        open(err, "w").close()
        write(state, "thinking\n")
        launch(key, 110, 30, sess, tmp, err, flags="")
        time.sleep(3.0)
        if not tmux_alive(sess):
            return record("verify", "seamless contract (thinking/idle/end)", False,
                          "died on launch | stderr: " + tail(read(err), 6))

        a = capture(sess); time.sleep(0.8); b = capture(sess)
        if a == b:
            ok = False; notes.append("FROZEN while state=thinking (two captures identical)")
        else:
            notes.append("runs while thinking (frames differ)")

        write(state, "idle\n")
        time.sleep(1.2)                       # past POLL_IDLE + one settling draw
        c = capture(sess); time.sleep(1.2); d = capture(sess)
        if c != d:
            ok = False; notes.append("STILL MOVING while state=idle")
        else:
            notes.append("freezes while idle (two captures identical)")

        write(state, "thinking\n")
        time.sleep(0.9)
        e = capture(sess); time.sleep(0.8); f = capture(sess)
        if e == f:
            ok = False; notes.append("did NOT resume on thinking")
        else:
            notes.append("resumes on thinking")

        write(state, "end\n")
        t0 = time.time()
        while time.time() - t0 < 1.0:
            if not tmux_alive(sess):
                break
            time.sleep(0.05)
        gone = not tmux_alive(sess)
        dt = time.time() - t0
        if not gone:
            ok = False; notes.append("still alive 1s after 'end'")
        else:
            notes.append("exited on end in %.2fs" % dt)
        if os.path.exists(state):
            ok = False; notes.append("left its state file behind")
        else:
            notes.append("state file removed")
        if read(err).strip():
            ok = False; notes.append("stderr: " + tail(read(err), 4))
        else:
            notes.append("stderr empty")
        return record("verify", "seamless contract (thinking/idle/end)", ok, "; ".join(notes))
    finally:
        tmux_kill(sess)
        shutil.rmtree(tmp, ignore_errors=True)


def verify(key):
    path = os.path.join(GAMES_DIR, key + ".py")
    if not os.path.exists(path):
        record("verify", "games/%s.py exists" % key, False, "no such file")
        return False
    record("verify", "games/%s.py exists" % key, True,
           "%d lines, tagline: %s" % (len(read(path).splitlines()), tagline(key)))
    ok = True
    ok &= v_compile(key)
    ok &= v_no_network(key)
    ok &= v_py39(key)
    ok &= v_mono_colour(key)
    ok &= v_hue_names(key)
    ok &= v_glyph_families()
    ok &= v_launch(key, 110, 30, "full")
    ok &= v_launch(key, 80, 8, "tiny")
    ok &= v_launch(key, 90, 26, "mono", "TERM=vt100 747_ASCII=1", wait=2.0)
    ok &= v_seamless(key)
    return bool(ok)


# ---------------------------------------------------------------------------
# 2. REGISTER — every edit surgical and idempotent
# ---------------------------------------------------------------------------
def r_catalogue(key, name, blurb, apply_):
    src = read(PICKER)
    rows = catalogue_rows(src)
    if key in [r[0] for r in rows]:
        return record("register", "picker CATALOGUE (games/breakout.py)", True,
                      "already registered as %s, %d titles" %
                      ([r[1] for r in rows if r[0] == key][0], len(rows)))
    m = re.search(r"(CATALOGUE = \[\n)(.*?)(^\])", src, re.S | re.M)
    if not m:
        return record("register", "picker CATALOGUE (games/breakout.py)", False,
                      "CATALOGUE block not found — the picker's shape changed")
    # Column widths copied from the rows already there: key and name fields are
    # each padded to 12 so the three columns line up. A malformed row here is a
    # SyntaxError in the picker, which is why final_gates() byte-compiles it.
    row = '    (%s%s"%s"),\n' % (('"%s",' % key).ljust(12), ('"%s",' % name).ljust(12), blurb)
    new = src[:m.end(2)] + row + src[m.end(2):]
    if apply_:
        write(PICKER, new)
    return record("register", "picker CATALOGUE (games/breakout.py)", True,
                  ("inserted" if apply_ else "WOULD insert") + " " + row.strip())


COMMAND_TEMPLATE = '''---
name: {key}
description: Toggle the {name} game pane in your terminal (free-play)
---

Run this exactly, then report the one-line result to the user:

```bash
bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/breakout-hook.sh" toggle {key}
```

This opens {name} — {pitch} — in a tmux pane split in the current window (free-play, so it ignores Claude's thinking state), or closes it if it's already open. It requires the session to be running inside tmux. If it isn't, tell the user the game needs a tmux session and that the auto-launch-while-thinking still works without the manual toggle.
'''


def r_command(key, name, apply_):
    path = os.path.join(COMMANDS, key + ".md")
    if os.path.exists(path):
        m = re.search(r'breakout-hook\.sh" toggle (\w+)', read(path))
        return record("register", "commands/%s.md" % key, m is not None and m.group(1) == key,
                      "already exists, toggles %s" % (m.group(1) if m else "NOTHING"))
    # The pitch sits inside an em-dash clause, so full stops from the tagline
    # would read as three sentences wedged mid-sentence. Flatten to one comma
    # list, matching how the shipped commands/*.md all read.
    clauses = [c.strip() for c in re.split(r"[.;]", tagline(key)) if c.strip()]
    pitch = ", ".join(c[0].lower() + c[1:] for c in clauses) or "a 747 Lab title"
    body = COMMAND_TEMPLATE.format(key=key, name=name, pitch=pitch)
    if apply_:
        write(path, body)
    return record("register", "commands/%s.md" % key, True,
                  ("wrote" if apply_ else "WOULD write") + " commands/%s.md (%d bytes)"
                  % (key, len(body)))


def r_whitelist(key, apply_):
    """GAMES= is a SECURITY GATE, not a convenience list: the chosen title flows
    into a file path and into the shell exec string handed to `tmux split-window`,
    so an un-whitelisted title is refused and silently falls back to breakout.
    That is exactly why a finished title looks "broken" instead of "unregistered".
    The header comment listing the slash commands is synced in the same pass —
    kept OUTSIDE the early return so it self-heals even on an already-listed
    title, instead of drifting the first time the two got out of step."""
    src = read(HOOK)
    m = re.search(r'^GAMES="([^"]+)"', src, re.M)
    if not m:
        return record("register", "hook whitelist (GAMES=)", False, "GAMES= line not found")
    games = m.group(1).split()
    new = src
    notes = []
    if key in games:
        notes.append("already whitelisted: %s" % " ".join(games))
    else:
        new = src[:m.start(1)] + " ".join(games + [key]) + src[m.end(1):]
        notes.append('GAMES="%s"' % " ".join(games + [key]))
    cm = re.search(r"^#\s+toggle\s+\(([^)]*)\)", new, re.M)
    if cm and ("/%s" % key) not in cm.group(1):
        new = new[:cm.end(1)] + ", /%s" % key + new[cm.end(1):]
        notes.append("header comment now lists /%s" % key)
    elif cm:
        notes.append("header comment already lists /%s" % key)
    if apply_ and new != src:
        write(HOOK, new)
    return record("register", "hook whitelist (GAMES=)", True,
                  ("%s: " % ("edited" if apply_ else "WOULD edit") if new != src
                   else "no change needed: ") + "; ".join(notes))


def r_ci(key, name, index, apply_):
    """Five title lists live in ci.yml. Each is matched by its own shape, so an
    unrelated edit elsewhere in the workflow cannot be hit by accident."""
    src = read(CI)
    orig = src
    did, skipped = [], []

    # (a) the shell launch / soak / resize matrices: `for g in a b c; do`
    def shell_list(mm):
        titles = mm.group(2).split()
        if key in titles:
            return mm.group(0)
        return "%sfor g in %s; do" % (mm.group(1), " ".join(titles + [key]))
    src, n = re.subn(r"( *)for g in ([a-z0-9_ ]+); do", shell_list, src)
    already = len(re.findall(r"for g in [a-z0-9_ ]*\b%s\b" % key, src))
    did.append("shell matrices: %d list(s), %d now contain %s" % (n, already, key))

    # (b) the colour-less step's module tuple
    def py_tuple(mm):
        if "'%s'" % key in mm.group(2):
            return mm.group(0)
        return "%s%s, '%s'%s" % (mm.group(1), mm.group(2).rstrip(), key, mm.group(3))
    src, n = re.subn(r"(for name in \()([^)]*?)(\):)", py_tuple, src)
    did.append("colour-less module tuple: %d" % n)

    # (c) the picker's expected on-screen names — insert BEFORE the menu header
    #     string so the title order still reads like the line order.
    if "'%s'" % name not in src:
        src, n = re.subn(r"(for t in \()([^)]*?)('CHOOSE YOUR GAME')",
                         lambda mm: "%s%s'%s', %s" % (mm.group(1), mm.group(2), name, mm.group(3)),
                         src)
        did.append("picker expected names: %d" % n)
    else:
        skipped.append("picker names already list %s" % name)

    # (d) the routing matrix: `route <n> <PANETITLE-> <key>`
    if not re.search(r"^ *route \d+ .* %s$" % key, src, re.M):
        rows = re.findall(r"^( *)route (\d+) (\S+)( +)(\w+)$", src, re.M)
        if rows:
            indent = rows[-1][0]
            last = re.search(r"^ *route %s .*$" % rows[-1][1], src, re.M)
            pane = ("%s747-" % name.upper())
            line = "\n%sroute %d %s%s" % (indent, index, pane.ljust(13), key)
            src = src[:last.end()] + line + src[last.end():]
            did.append("routing matrix: route %d %s -> %s" % (index, pane, key))
        else:
            skipped.append("no route rows found")
    else:
        skipped.append("routing already covers %s" % key)

    # (e) step names that hard-code a title COUNT go stale on every release.
    #     Make them count-free once, so they never need this script again.
    src = src.replace("All four soak for 60 s side by side",
                      "All titles soak for 60 s side by side")
    src = src.replace("The picker offers all four titles at every geometry",
                      "The picker offers every title at every geometry")

    changed = src != orig
    if apply_ and changed:
        write(CI, src)
    return record("register", ".github/workflows/ci.yml matrices", True,
                  ("%s: " % ("edited" if apply_ else "WOULD edit") if changed
                   else "no change needed: ") + "; ".join(did + skipped))


def r_manifest_meta(key, apply_):
    """The manifests advertise the line-up in prose ("Four titles: ..."). A new
    title makes that copy WRONG, which is a shipped-facing defect, so it is a
    registration point too. Rebuilt from CATALOGUE, never hand-counted."""
    rows = catalogue_rows()
    names = [r[1].title() for r in rows]
    count = NUMWORD.get(len(names), str(len(names)))
    listed = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    want_tail = "%s titles: %s." % (count, listed)
    notes = []
    for path, getter in ((PLUGIN_JSON, None), (MARKET_JSON, None)):
        raw = read(path)
        new = re.sub(r"(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten) titles: [^\"]*?\.",
                     want_tail, raw)
        if key not in new and '"keywords"' in new:
            new = re.sub(r'("keywords": \[)', r'\1', new)
            new = re.sub(r'("keywords": \[[^\]]*?)\]',
                         lambda mm: mm.group(1) + ', "%s"]' % key, new)
        if new != raw:
            try:
                json.loads(new)
            except ValueError as e:
                return record("register", "manifest line-up copy", False,
                              "%s would become invalid JSON: %s" % (path, e))
            if apply_:
                write(path, new)
            notes.append("%s updated" % os.path.basename(path))
        else:
            notes.append("%s already current" % os.path.basename(path))
    return record("register", "manifest line-up copy + keywords", True,
                  '%s -> "%s"' % ("; ".join(notes), want_tail))


def r_readme(key, name):
    """README is DOCUMENTATION, not a registration point — nothing breaks if it
    lags. Report it so it never silently goes stale; never edit it blind."""
    txt = read(README)
    hit = key in txt or name in txt
    return record("register", "README mentions the title (report only)", True,
                  "README already names %s" % name if hit else
                  "NOT mentioned — README is prose, edit it by hand (not a wiring point)")


# ---------------------------------------------------------------------------
# 3. VERSIONS
# ---------------------------------------------------------------------------
def versions(new_version, apply_):
    p_raw, m_raw = read(PLUGIN_JSON), read(MARKET_JSON)
    pv = json.loads(p_raw)["version"]
    mv = json.loads(m_raw)["plugins"][0]["version"]
    if new_version is None:
        ok = pv == mv
        return record("version", "manifests agree (no bump requested)", ok,
                      "plugin.json=%s marketplace.json=%s" % (pv, mv))
    if not re.match(r"^\d+\.\d+\.\d+$", new_version):
        return record("version", "bump to %s" % new_version, False, "not semver X.Y.Z")
    if pv == new_version and mv == new_version:
        return record("version", "bump to %s" % new_version, True, "both already at %s" % new_version)
    p_new = p_raw.replace('"version": "%s"' % pv, '"version": "%s"' % new_version, 1)
    m_new = m_raw.replace('"version": "%s"' % mv, '"version": "%s"' % new_version, 1)
    for raw, new in ((p_raw, p_new), (m_raw, m_new)):
        if raw == new:
            return record("version", "bump to %s" % new_version, False,
                          "version string not found to replace")
        try:
            json.loads(new)
        except ValueError as e:
            return record("version", "bump to %s" % new_version, False, "invalid JSON: %s" % e)
    if apply_:
        write(PLUGIN_JSON, p_new)
        write(MARKET_JSON, m_new)
    return record("version", "bump to %s (lockstep)" % new_version, True,
                  "%s: %s -> %s, %s -> %s" % ("bumped" if apply_ else "WOULD bump",
                                              pv, new_version, mv, new_version))


# ---------------------------------------------------------------------------
# 4. FINAL GATES
# ---------------------------------------------------------------------------
def g_picker_compiles():
    """WE EDIT THE PICKER'S SOURCE, so we byte-compile it. Learned the hard way:
    the first version of r_catalogue() dropped the opening paren of the tuple and
    wrote a SyntaxError into games/breakout.py — the whole plugin, dead, from a
    registration step that reported GREEN."""
    rc, out = run([PY, "-m", "py_compile", "games/breakout.py"])
    return record("gate", "py_compile games/breakout.py (we edited it)", rc == 0,
                  "picker still compiles" if rc == 0 else tail(out, 5))


def g_picker_offers(key, name):
    """REACHABILITY, PROVEN ON A REAL SCREEN. Everything else is static analysis;
    this launches the picker in a pane, reads the menu back, and requires the new
    title to be on it — under 747_ASCII=1, which is the render that has broken
    before. Also asserts the row is SELECTABLE (numbered) and pure ASCII."""
    tmp = tempfile.mkdtemp(prefix="rel747-pick-")
    sess = "pick_%s" % key
    err = os.path.join(tmp, "err")
    try:
        write(os.path.join(tmp, "state-%s" % sess), "thinking\n")
        cmd = ("env BREAKOUT747_STATE=%s TERM=vt100 747_ASCII=1 %s games/breakout.py "
               "--ask --session %s 2>%s" % (tmp, PY, sess, err))
        tmux_kill(sess)
        tmux("new-session", "-d", "-s", sess, "-x", "100", "-y", "24", cmd)
        time.sleep(9)                       # past the flyby intro, into the ask
        menu = capture(sess)
        write(os.path.join(tmp, "state-%s" % sess), "end\n")
        time.sleep(1)
        rows = [r[0] for r in catalogue_rows()]
        idx = rows.index(key) + 1 if key in rows else 0
        fail = []
        if name not in menu:
            fail.append("%s is not on the menu" % name)
        if "[%d]" % idx not in menu:
            fail.append("no [%d] selector for it" % idx)
        if any(ord(c) > 127 for c in menu):
            fail.append("non-ASCII byte on screen under 747_ASCII=1")
        if not menu.strip():
            fail.append("captured an empty pane | stderr: " + tail(read(err), 4))
        line = next((l.strip() for l in menu.splitlines() if name in l), "")
        return record("gate", "picker offers %s on a live screen" % name, not fail,
                      "; ".join(fail) if fail else "menu row: %r" % line)
    finally:
        tmux_kill(sess)
        shutil.rmtree(tmp, ignore_errors=True)


def final_gates(key, name):
    ok = True
    ok &= g_picker_compiles()
    for target in (".claude-plugin/plugin.json", "."):
        rc, out = run(["claude", "plugin", "validate", "--strict", target])
        ok &= record("gate", "claude plugin validate --strict %s" % target, rc == 0,
                     tail(out, 4))
    # The wiring assertion ci.yml calls "The line is wired end to end", run here
    # so the pipeline proves its OWN edits instead of trusting them.
    src = '''
import os, re, sys
fail = []
disk = sorted(f[:-3] for f in os.listdir("games") if f.endswith(".py"))
src = open("games/breakout.py").read()
block = re.search(r"CATALOGUE = \\[(.*?)^\\]", src, re.S | re.M).group(1)
rows = re.findall(r'\\(\\s*"([a-z0-9_]+)"\\s*,\\s*"([^"]+)"\\s*,\\s*"([^"]*)"\\s*\\)', block)
keys = [r[0] for r in rows]
wl = re.search(r'^GAMES="([^"]+)"', open("hooks/breakout-hook.sh").read(), re.M).group(1).split()
targets = {}
for f in sorted(os.listdir("commands")):
    m = re.search(r'breakout-hook\\.sh" toggle (\\w+)', open(os.path.join("commands", f)).read())
    if m:
        targets.setdefault(m.group(1), []).append(f)
if sorted(keys) != disk:
    fail.append("picker CATALOGUE %s != disk %s" % (sorted(keys), disk))
if sorted(wl) != disk:
    fail.append("hook whitelist %s != disk %s" % (sorted(wl), disk))
for k in disk:
    if k not in targets:
        fail.append('no slash command opens "%s"' % k)
for key, name, blurb in rows:
    if "747" in name:
        fail.append("display name carries a 747 suffix: " + name)
if re.search(r"preview", src, re.I):
    fail.append('the word "preview" is back in breakout.py')
for f in fail:
    print("FAIL", f)
if not fail:
    print("disk=%s picker=%s whitelist=%s commands=%s"
          % (disk, keys, wl, sorted(targets)))
sys.exit(1 if fail else 0)
'''
    rc, out = py_snippet(src)
    ok &= record("gate", "line wired end to end (ci.yml assertion)", rc == 0, tail(out, 4))
    rc, out = run(["bash", "-n", "hooks/breakout-hook.sh"])
    ok &= record("gate", "bash -n hooks/breakout-hook.sh", rc == 0, tail(out, 3) if rc else "clean")
    for j in ("hooks/hooks.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        try:
            json.loads(read(os.path.join(ROOT, j)))
            good, why = True, "valid JSON"
        except ValueError as e:
            good, why = False, str(e)
        ok &= record("gate", "JSON parses: %s" % j, good, why)
    ok &= g_picker_offers(key, name)
    return bool(ok)


# ---------------------------------------------------------------------------
def summary():
    print("\n" + "=" * 96)
    print("%-9s %-52s %s" % ("STATUS", "STEP", "PHASE"))
    print("-" * 96)
    for phase, step, ok, _ in RESULTS:
        print("%-9s %-52s %s" % ("GREEN" if ok else "RED", step, phase))
    print("-" * 96)
    reds = [r for r in RESULTS if not r[2]]
    print("%d steps: %d GREEN, %d RED" % (len(RESULTS), len(RESULTS) - len(reds), len(reds)))
    for phase, step, _, ev in reds:
        print("  RED  [%s] %s :: %s" % (phase, step, ev.replace("\n", " | ")[:600]))
    print("=" * 96)
    return not reds


def main():
    ap = argparse.ArgumentParser(
        description="Verify and register a 747 Terminal Games title. Never runs git.")
    ap.add_argument("key", help="title key == games/<key>.py")
    ap.add_argument("--version", default=None, help="bump both manifests to X.Y.Z, in lockstep")
    ap.add_argument("--check-only", action="store_true",
                    help="verify and REPORT what registration would change; write nothing")
    ap.add_argument("--name", default=None, help="picker display name (default: KEY, no 747 suffix)")
    ap.add_argument("--blurb", default=None,
                    help="picker blurb; use the token {sep}, never a literal separator")
    a = ap.parse_args()

    key = a.key.strip()
    if not re.match(r"^[a-z0-9_]+$", key):
        print("RED  title key must match [a-z0-9_]+ (it becomes a file path and a shell word)")
        return 2
    name = a.name or key.upper()
    if "747" in name:
        print("RED  display names carry NO 747 suffix — the 747 lives inside the game")
        return 2
    blurb = a.blurb or blurb_for(key)
    apply_ = not a.check_only

    if not shutil.which("tmux"):
        print("RED  tmux not on PATH — the launch and contract gates cannot run")
        return 2

    print("747 Terminal Games :: release-title %s" % key)
    print("repo   : %s" % ROOT)
    print("mode   : %s" % ("CHECK-ONLY (no writes)" if a.check_only else "APPLY"))
    print("title  : key=%s name=%s\n         blurb=%r\n" % (key, name, blurb))

    print("--- 1. VERIFY " + "-" * 60)
    verify(key)

    print("\n--- 2. REGISTER " + "-" * 58)
    r_catalogue(key, name, blurb, apply_)
    r_command(key, name, apply_)
    r_whitelist(key, apply_)
    index = [r[0] for r in catalogue_rows()].index(key) + 1 \
        if key in [r[0] for r in catalogue_rows()] else len(catalogue_rows()) + 1
    r_ci(key, name, index, apply_)
    r_manifest_meta(key, apply_)
    r_readme(key, name)

    print("\n--- 3. VERSIONS " + "-" * 58)
    versions(a.version, apply_)

    print("\n--- 4. FINAL GATES " + "-" * 55)
    final_gates(key, name)

    all_green = summary()
    if a.check_only:
        print("check-only: nothing was written. Re-run without --check-only to apply.")
    print("This script ran NO git commands. Review the working tree, then commit.")
    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
