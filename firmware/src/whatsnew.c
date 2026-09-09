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

const char *whatsnew_lines_at(int i)
{
	return (i >= 0 && i < N_NOTES) ? NOTES[i].lines : "";
}

int whatsnew_has(const char *version)
{
	return index_of(version) >= 0;
}

/* Rows a table entry will occupy: one per line. The copy rules keep every
 * line inside the label's width, so a line is a row and neither wraps. */
static int rows_in(const char *s)
{
	int n = 1;

	for (; *s; s++) {
		if (*s == '\n') {
			n++;
		}
	}
	return n;
}

int whatsnew_render(const char *from, const char *to, char *buf, size_t len)
{
	return whatsnew_render_rows(from, to, buf, len, WHATSNEW_ROWS);
}

int whatsnew_render_rows(const char *from, const char *to, char *buf,
			 size_t len, int rows)
{
	int start = index_of(to);
	size_t used = 0;
	int shown = 0, dropped = 0;

	if (!buf || len == 0) {
		return 0;
	}
	buf[0] = '\0';
	if (start < 0) {
		/* A version with no entry. Say nothing rather than guess:
		 * the caller's header still names the version. */
		return 0;
	}

	/*
	 * How far back to go. An empty or unparseable `from` means the board
	 * cannot tell where it came from -- a breadcrumb written before
	 * whatsnew_trail existed -- and the honest answer is the one release
	 * we know it is running.
	 */
	int last = start;

	if (from && from[0]) {
		for (int i = start; i < N_NOTES; i++) {
			if (!ota_version_newer(NOTES[i].version, from)) {
				break;
			}
			last = i;
		}
	}

	/*
	 * Room held back for the count line, whenever there is more than one
	 * release in play and therefore something that could be dropped.
	 *
	 * Without the reservation the count is the first thing squeezed out,
	 * which is precisely backwards: it is the line that stops the popup
	 * from silently claiming the newest release is all that changed. A
	 * 64-byte render of the 1.2.5 -> 1.3.2 jump spent every byte on 1.3.2
	 * and then had no room to say that two more releases existed.
	 */
	size_t room = len;
	int row_room = rows;
	int rows_used = 0;

	if (last > start) {
		/* Sized for a four-digit count, not a two-digit one. At 100
		 * releases the reservation was one byte short of the line it
		 * was reserving for, so the count would be silently dropped
		 * -- the exact failure the reservation exists to prevent,
		 * appearing only once there were enough releases to need it
		 * most. Found by building a 103-release table and looking at
		 * the panel; three bytes of a 224-byte budget. */
		const size_t tail_max = sizeof("\nand 9999 earlier updates.");

		room = len > tail_max ? len - tail_max : len;
		/* The count line is a row too, and it is the one row that
		 * must never be the one dropped. */
		if (row_room > 1) {
			row_room--;
		}
	}

	for (int i = start; i <= last; i++) {
		size_t need = strlen(NOTES[i].lines) + 1;   /* + '\n' */
		int need_rows = rows_in(NOTES[i].lines);

		/*
		 * Rows first, and ABSOLUTELY -- no first-entry exception.
		 *
		 * Overflowing the byte budget costs a sentence nobody reads.
		 * Overflowing the row budget costs the OK button, which is
		 * clamped out of the box's 230 px with scrolling cleared, and
		 * a popup that cannot be dismissed is a board the customer
		 * cannot get past. The two budgets are not the same kind of
		 * limit and they do not get the same kind of exception.
		 */
		if (rows_used + need_rows > row_room) {
			dropped = last - i + 1;
			break;
		}
		/*
		 * Newest first, and the budget stops the walk rather than
		 * truncating a line mid-sentence. Half a sentence about a
		 * feature is worse than a count of the sentences that did not
		 * fit -- the customer cannot tell the difference between a
		 * clipped line and a badly written one.
		 *
		 * The BYTE reservation, unlike the row cap, never costs the
		 * first entry: with room for one release or a count but not
		 * both, the words win, because "and 2 earlier updates." on
		 * its own tells a customer nothing they can use.
		 */
		if (used + need >= (shown ? room : len)) {
			dropped = last - i + 1;
			break;
		}
		rows_used += need_rows;
		if (used) {
			buf[used++] = '\n';
		}
		memcpy(buf + used, NOTES[i].lines, strlen(NOTES[i].lines));
		used += strlen(NOTES[i].lines);
		buf[used] = '\0';
		shown++;
	}

	/*
	 * Nothing is said about releases OLDER than the oldest entry here.
	 *
	 * The table cannot tell whether any exist: between 1.2.5 and 1.3.0
	 * there were none, so a customer on that jump who was told "and
	 * earlier updates" would be reading an invention. The only count this
	 * is entitled to make is the one below, of entries it has and could
	 * not fit.
	 */
	if (dropped > 0) {
		char tail[40];
		int n = snprintf(tail, sizeof(tail),
				 "%sand %d earlier update%s.",
				 used ? "\n" : "", dropped,
				 dropped == 1 ? "" : "s");

		if (n > 0 && used + (size_t)n < len) {
			memcpy(buf + used, tail, (size_t)n + 1);
		}
	}
	return shown;
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
