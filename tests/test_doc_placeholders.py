"""No unfilled placeholder may reach a published document.

README.md is the first thing a customer sees and the repository is public, so a
link that reads MAKERWORLD_URL_PENDING is not a private note -- it is a broken
promise on the shop window, and the kind that survives for months because the
person who left it is the one person who stops seeing it.

The marker was introduced on 2026-09-12 while the MakerWorld listing was in
review: the sentence about the case files was written before the URL existed,
deliberately, so that adding the link later is one edit rather than one edit
plus remembering where it goes. This test is the other half of that bargain.

Scope is every tracked .md file rather than README.md alone, because the same
trick will be used again and the next one will be somewhere else.
"""
import pathlib
import re
import subprocess

# The marker to search for, and what the fix is. Keeping the two together means
# a failure tells the reader what to do instead of only what is wrong.
PLACEHOLDERS = {
    "MAKERWORLD_URL_PENDING":
        "the MakerWorld listing is published -- paste its URL here",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _tracked_markdown():
    """Markdown files git knows about.

    Tracked rather than globbed: an untracked scratch file in the working tree
    is nobody's business, and the scratchpad this session used is full of
    drafts that would fail for no reason.
    """
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.md"],
                         capture_output=True, text=True)
    if out.returncode:
        return []
    return [ROOT / line for line in out.stdout.split("\n") if line]


def test_no_placeholder_survives_into_a_tracked_document():
    found = []
    for path in _tracked_markdown():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker, fix in PLACEHOLDERS.items():
            for n, line in enumerate(text.split("\n"), 1):
                if marker in line:
                    rel = path.relative_to(ROOT)
                    found.append("%s:%d  %s  -> %s" % (rel, n, marker, fix))

    assert not found, (
        "placeholder left in a tracked document:\n  "
        + "\n  ".join(found))


def test_the_marker_is_one_git_grep_can_find():
    """A placeholder nobody can search for is worse than none.

    The rule the marker has to obey is that it contains no regex metacharacter
    and no whitespace, so `git grep MAKERWORLD_URL_PENDING` finds every
    instance with no quoting and no escaping. Pinned because the obvious way to
    write the next one -- <fill me in> -- breaks both halves of that.
    """
    for marker in PLACEHOLDERS:
        assert marker == marker.strip(), marker
        assert not re.search(r"[\s\\^$.|?*+()\[\]{}]", marker), marker
        assert marker.isupper(), marker
