#include <stdio.h>
#include <string.h>

#include "whatsnew.h"
#include "ota_parse.h"

/*
 * One entry per shipped release, NEWEST FIRST.
 *
 * House rules for the text, because this is customer-facing copy on a panel
 * and not a changelog:
 *
 *   - Sentence case, like every other string on this screen.
 *   - One line per change, "\n" between them, no trailing newline.
 *   - About 30 characters before the 270 px label wraps. A line may wrap;
 *     three wrapping lines will not fit beside two other releases.
 *   - Two changes per release is the working limit. Pick the two a customer
 *     would notice, not the two that were hardest.
 *   - What they GET, not what was repaired. "Finished sessions leave the
 *     panel" beats "fixed a session leak on Linux".
 *
 * This does not have to remember every release forever. A jump that starts
 * below the oldest entry simply gets every entry there is, which is all this
 * table is entitled to claim -- it cannot know whether releases existed below
 * itself. Trimming the tail is therefore a deliberate act, and the thing to
 * weigh is whether a customer who has been away a couple of months still
 * sees words.
 */
static const struct {
	const char *version;
	const char *lines;
} NOTES[] = {
	{ "1.3.2", "Updates report honestly\nFinished sessions clear away" },
	{ "1.3.1", "It spots an out-of-date app" },
	{ "1.3.0", "Claude Desktop countdowns\nA pip for every session" },
};

#define N_NOTES ((int)(sizeof(NOTES) / sizeof(NOTES[0])))

static int index_of(const char *version)
{
	if (!version || !version[0]) {
		return -1;
	}
	for (int i = 0; i < N_NOTES; i++) {
		if (strcmp(NOTES[i].version, version) == 0) {
			return i;
		}
	}
	return -1;
}

int whatsnew_entries(void)
{
	return N_NOTES;
}

const char *whatsnew_version_at(int i)
{
	return (i >= 0 && i < N_NOTES) ? NOTES[i].version : "";
}

const char *whatsnew_lines_at(int i)
{
	return (i >= 0 && i < N_NOTES) ? NOTES[i].lines : "";
}

int whatsnew_has(const char *version)
{
	return index_of(version) >= 0;
}

/* Lines in an entry: one per '\n', plus one. */
static int lines_in(const char *s)
{
	int n = 1;

	for (; *s; s++) {
		if (*s == '\n') {
			n++;
		}
	}
	return n;
}

/* Pixels an entry occupies, not counting the gap above it. */
static int height_of(int i)
{
	return WHATSNEW_VER_PX + lines_in(NOTES[i].lines) * WHATSNEW_LINE_PX;
}

/*
 * The span of releases to describe: newest first, from `to` back to but not
 * including `from`. Returns the count, and sets *start.
 *
 * An unknown `from` is not an error -- it is what a breadcrumb written
 * before whatsnew_trail existed reports -- and the honest answer is the one
 * release we know the board is running.
 */
static int span(const char *from, const char *to, int *start)
{
	int s = index_of(to);
	int last;

	*start = s;
	if (s < 0) {
		return 0;
	}
	last = s;
	if (from && from[0]) {
		for (int i = s; i < N_NOTES; i++) {
			if (!ota_version_newer(NOTES[i].version, from)) {
				break;
			}
			last = i;
		}
	}
	return last - s + 1;
}

int whatsnew_paginate(const char *from, const char *to,
		      struct whatsnew_page *out, int max)
{
	return whatsnew_paginate_px(from, to, out, max, WHATSNEW_PAGE_PX);
}

int whatsnew_paginate_px(const char *from, const char *to,
			 struct whatsnew_page *out, int max, int page_px)
{
	int start, n = span(from, to, &start);
	int pages = 0, used = 0, on_page = 0, first = start;

	if (n <= 0) {
		return 0;
	}
	for (int i = start; i < start + n; i++) {
		int need = height_of(i) + (on_page ? WHATSNEW_GAP_PX : 0);

		/*
		 * A release never straddles a page turn, so a release that
		 * does not fit starts the next page rather than being split.
		 * The first release on a page is admitted unconditionally:
		 * WHATSNEW_LINES_PER_ENTRY keeps any one of them well under a
		 * page, and a release that could not fit alone would
		 * otherwise loop forever here.
		 */
		if (on_page && used + need > page_px) {
			if (pages < max && out) {
				out[pages].first = first;
				out[pages].count = on_page;
			}
			pages++;
			first = i;
			on_page = 0;
			used = 0;
			need = height_of(i);
		}
		used += need;
		on_page++;
	}
	if (on_page) {
		if (pages < max && out) {
			out[pages].first = first;
			out[pages].count = on_page;
		}
		pages++;
	}
	return pages;
}

int whatsnew_summary(const char *from, const char *to, char *buf, size_t len)
{
	int start, n = span(from, to, &start);
	int changes = 0;

	if (!buf || len == 0) {
		return 0;
	}
	buf[0] = '\0';
	if (n <= 0) {
		return 0;
	}
	for (int i = start; i < start + n; i++) {
		changes += lines_in(NOTES[i].lines);
	}
	/*
	 * "since 1.2.5" only when it says something. With one release it is a
	 * comparison nobody asked for -- "2 changes since 1.3.1" on an update
	 * from 1.3.1 -- and with an unknown origin it would be a version we
	 * are not entitled to name.
	 */
	if (n > 1 && from && from[0]) {
		snprintf(buf, len, "%d change%s since %s", changes,
			 changes == 1 ? "" : "s", from);
	} else {
		snprintf(buf, len, "%d change%s", changes,
			 changes == 1 ? "" : "s");
	}
	return changes;
}

void whatsnew_trail(const char *from, const char *to, char *buf, size_t len)
{
	if (!buf || len == 0) {
		return;
	}
	if (!to) {
		to = "";
	}
	/* strlen(from) + 1 for '>' + strlen(to) + 1 for NUL. Snprintf would
	 * TRUNCATE rather than refuse, and a truncated version string is a
	 * breadcrumb that names a release nobody shipped. */
	if (from && from[0] && !strchr(from, '>') &&
	    strlen(from) + strlen(to) + 2 <= len) {
		snprintf(buf, len, "%s>%s", from, to);
		return;
	}
	snprintf(buf, len, "%s", to);
}

void whatsnew_split(const char *trail, char *from, size_t flen,
		    char *to, size_t tlen)
{
	const char *sep;

	if (from && flen) {
		from[0] = '\0';
	}
	if (to && tlen) {
		to[0] = '\0';
	}
	if (!trail) {
		return;
	}
	sep = strchr(trail, '>');
	if (!sep) {
		if (to && tlen) {
			snprintf(to, tlen, "%s", trail);
		}
		return;
	}
	if (from && flen) {
		size_t n = (size_t)(sep - trail);

		if (n >= flen) {
			n = flen - 1;
		}
		memcpy(from, trail, n);
		from[n] = '\0';
	}
	if (to && tlen) {
		snprintf(to, tlen, "%s", sep + 1);
	}
}
