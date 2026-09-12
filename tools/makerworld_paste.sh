#!/bin/sh
# Render the MakerWorld listing for pasting.
#
#   sh tools/makerworld_paste.sh [outfile]
#
# MakerWorld's Description box is a WYSIWYG editor, not a markdown field: paste
# docs/makerworld.md into it raw and the reader sees literal ## and **. What it
# does accept is formatted HTML off the clipboard, so this renders the listing
# (everything between the two comment blocks) and writes a plain page to copy
# from.
#
# Uses `gh api /markdown` rather than a markdown library, because pc/ is
# standard library only and this has to agree with what GitHub shows for the
# same file.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
OUT="${1:-$HOME/Downloads/blink-makerworld-paste.html}"

command -v gh >/dev/null 2>&1 || {
	echo "needs the gh CLI, logged in: gh auth login" >&2
	exit 1
}

SRC="$ROOT/docs/makerworld.md" OUT="$OUT" python3 - <<'PY'
import json, os, pathlib, re, subprocess

src = pathlib.Path(os.environ["SRC"])
md = src.read_text(encoding="utf-8")
# Between the header comment and the *** rule: the part that gets pasted. The
# notes after the rule are ours and must never reach the page.
body = md.split("-->", 1)[1].split("\n***\n", 1)[0].strip()
for leak in ("BEFORE PUBLISHING", "THE LICENCE", "<!--"):
    assert leak not in body, "internal notes leaked into the listing: " + leak

r = subprocess.run(["gh", "api", "--method", "POST", "/markdown", "--input", "-"],
                   input=json.dumps({"mode": "markdown", "text": body}),
                   capture_output=True, text=True)
if r.returncode:
    raise SystemExit("gh api /markdown failed: " + r.stderr[:300])

html = r.stdout
# GitHub hangs an anchor icon off every heading; pasted, they arrive as stray
# links. The class attributes go too -- the clipboard wants plain semantic HTML
# the editor can map onto its own styles.
html = re.sub(r'<a[^>]*class="[^"]*anchor[^"]*"[^>]*>.*?</a>', "", html, flags=re.S)
html = re.sub(r'\s(class|id|dir)="[^"]*"', "", html)

page = '''<meta charset="utf-8"><title>BLINK - paste into MakerWorld</title>
<style>
 body{background:#fff;color:#111;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:28px;max-width:820px}
 h1{font-size:26px;margin:0 0 12px} h2{font-size:20px;margin:26px 0 8px}
 li{margin:3px 0} code{background:#f0f0f0;padding:1px 4px;border-radius:3px}
 #note{background:#fffbe6;border:1px solid #e6d48a;padding:10px 14px;border-radius:6px;
       margin:0 0 22px;font-size:13px;color:#5a4a00}
</style>
<div id="note"><b>Select from the line below to the end</b> (click just before "BLINK",
then Cmd-Shift-End), copy, and paste into MakerWorld's Description box. Do not include
this note. Headings, bold and bullets carry across.</div>
<hr>
''' + html

out = pathlib.Path(os.environ["OUT"])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(page, encoding="utf-8")
print("wrote %s  (%d headings, %d lists, %d bold runs)"
      % (out, html.count("<h2"), html.count("<ul"), html.count("<strong")))
PY
