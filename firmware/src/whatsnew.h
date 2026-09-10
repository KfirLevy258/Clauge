#ifndef WHATSNEW_H
#define WHATSNEW_H

#include <stddef.h>

/*
 * What the popup after an update says, beyond the version number.
 *
 * It used to say one sentence -- "Updated to version 1.3.2." -- which tells
 * a customer that something happened and nothing about what. The release
 * notes exist (docs/whats-new-since-1.2.5.md), but they live on a website
 * nobody is looking at while holding the thing that just changed.
 *
 * Three constraints shaped this, and they are worth stating because they
 * rule out the obvious designs:
 *
 *   1. THE SCREEN. ui_settings_notice() is a 300 px box with a 270 px label
 *      and an OK button under it, clamped to 230 px tall. That is about
 *      eight lines of roughly 30 characters: a paragraph, not a page. The
 *      full notes for 1.2.5 -> 1.3.2 run to 153 lines of Markdown.
 *
 *   2. THE WIRE. Notes fetched from the daemon would cross a link that
 *      drops any line over 512 bytes whole and silently, where a loaded
 *      usage frame already measures 484 -- and they would arrive only on a
 *      machine whose daemon is alive, which is exactly the machine that
 *      does not need help. So the text is compiled in. It costs flash it
 *      would be dishonest to call free, and it is correct with no cable.
 *
 *   3. THE JUMP. Updates are not taken one at a time. The customer who
 *      prompted this went 1.2.5 -> 1.3.2, three releases in one tap, and a
 *      popup describing only the last of them would be describing the least
 *      of what changed for them.
 *
 * So: one short entry per release, compiled in, and a renderer that walks
 * every release between where the board was and where it landed, spending
 * the screen budget newest-first and saying how many it could not fit.
 *
 * tools/release.sh refuses to publish a version with no entry here. A notes
 * table that is allowed to go stale is worse than no notes table: it does
 * not degrade to silence, it degrades to describing the wrong release.
 */

/*
 * ---------------------------------------------------------------------
 * THE NOTICE says how much changed. THE SCREEN says what.
 * ---------------------------------------------------------------------
 *
 * An earlier version of this put the change list in the notice itself and
 * spent a long time on how many rows would fit. That was the wrong argument
 * to be having: the notice box is clamped to 230 px with
 * LV_OBJ_FLAG_SCROLLABLE cleared, so a row too many is not clipped and not
 * scrolled to -- it lands under the OK button, off the panel, on a popup
 * that is the only way past it. Every number in that budget was an estimate,
 * and being wrong cost the dismiss button.
 *
 * A screen has no such ceiling. It is 152 px of body against the notice's
 * 126, it is left-aligned because there is no box around it, and it can page
 * -- which is the answer this hardware wants, because NOTHING on this board
 * scrolls. ui_swipe.h has the measurement: five deliberate swipes on this
 * resistive panel produced thirty press-release cycles.
 */

/* Body height of the What's new screen: 240 less 12 top pad, a 20 px title,
 * an 8 px gap, the 36 px Back button and 12 px bottom pad. */
#define WHATSNEW_PAGE_PX 152

/* What a release costs on that screen: a dim version label, then one line
 * per change, plus a gap above every release but the first on its page. */
#define WHATSNEW_VER_PX  15
#define WHATSNEW_LINE_PX 17
#define WHATSNEW_GAP_PX   4

/* The most lines any single entry may carry. Enforced by the host test: it
 * is a rule about the copy someone writes at release time, and it is what
 * keeps a release from ever being too tall for a page of its own. */
#define WHATSNEW_LINES_PER_ENTRY 2

/* Longest summary this writes, plus room: "999 changes since 10.10.10". */
#define WHATSNEW_SUMMARY_MAX 48

/*
 * One page of the screen: which releases it holds.
 *
 * Pages break BETWEEN releases and never inside one, so a release and its
 * version label are never split across a page turn. The cost is that pages
 * are not equal length and the last is usually short, which is honest --
 * padding them would misrepresent where the boundaries are.
 */
struct whatsnew_page {
	int first;		/* index of the first release on this page */
	int count;		/* how many releases it holds */
};

/*
 * How many pages the screen has room to hold, and therefore the most this
 * table may ever grow to.
 *
 * The screen paginates into an array of this size on the stack. A page holds
 * at least one release, so the page count can never exceed the number of
 * entries in the table -- which means one number bounds both, and the host
 * test asserts the table against it. That assertion is the point: without it
 * the ceiling is a number in ui_settings.c that the table quietly grows past,
 * and the failure is not a crash. whatsnew_paginate returns the TRUE total
 * because the pager prints it, so the screen would say "9 / 12" and then draw
 * a blank body for the pages it had no room to describe.
 *
 * Sixteen because a customer would have to be sixteen releases behind to
 * reach it, and because the answer when the table does get there is to drop
 * the oldest entries rather than to widen this -- nobody updating from that
 * far back reads to the end, and the full notes are on the website.
 */
#define WHATSNEW_MAX_PAGES 16

/*
 * Pack the releases between `from` and `to` into pages.
 *
 * Returns the TOTAL number of pages, which may exceed `max` -- the caller
 * needs the true count for its "3 / 4", and truncating it silently would
 * make the pager lie. Fills at most `max` entries of `out`.
 *
 * An empty or unparseable `from` is not an error: it yields the single
 * release the board is running, which is all a breadcrumb from before this
 * existed can tell us.
 */
int whatsnew_paginate(const char *from, const char *to,
		      struct whatsnew_page *out, int max);

/* The same with the page height given explicitly, so the host test can prove
 * the packing at sizes it can construct rather than only asserting that
 * today's three-entry table happens to land on one page. */
int whatsnew_paginate_px(const char *from, const char *to,
			 struct whatsnew_page *out, int max, int page_px);

/*
 * "5 changes since 1.2.5", for the notice, into a WHATSNEW_SUMMARY_MAX buf.
 *
 * Writes "" and returns 0 when there is nothing to say -- a version with no
 * entry -- and the caller then shows its title alone. `since` is dropped
 * when `from` is unknown or when only one release is involved, because
 * "2 changes since 1.3.1" reads as a comparison nobody asked for.
 */
int whatsnew_summary(const char *from, const char *to, char *buf, size_t len);

/* The table, for the screen to draw and for the host test to measure. */
int whatsnew_entries(void);
const char *whatsnew_version_at(int i);
const char *whatsnew_lines_at(int i);

/* Is there an entry for this version? tools/release.sh asks, through the
 * host test, so that a release cannot ship without one. */
int whatsnew_has(const char *version);

/*
 * The breadcrumb written to cfg_store just before rebooting into a new
 * image, so the next boot can tell the customer what they gained.
 *
 * "<from>><to>", or just "<to>" when the pair does not fit. The record's
 * version slot is CFG_OTA_VER_MAX (16) bytes and it is NOT widened here on
 * purpose: growing struct rec means another migration of a record that is
 * already on shipped hardware, and the whole of what that would buy is a
 * cosmetic popup. Two three-part versions and a separator fit comfortably
 * while each part stays under two digits ("1.2.5>1.3.2" is 11 of the 15
 * usable bytes, and even "10.10.0>10.11.0" is 15) and beyond that this
 * silently records the target alone -- which is exactly what every release
 * before this one recorded, so the failure mode is the old behaviour rather
 * than a broken one.
 */
void whatsnew_trail(const char *from, const char *to, char *buf, size_t len);

/*
 * Split a breadcrumb back apart. `from` comes back empty for one written
 * before this existed, or written by the fallback above.
 *
 * Every reader must go through this. main.c compares the breadcrumb against
 * BLINK_FW_VERSION to decide whether the update landed, and handed the
 * packed form it would compare "1.2.5>1.3.2" against "1.3.2" and announce a
 * successful update as a failure.
 */
void whatsnew_split(const char *trail, char *from, size_t flen,
		    char *to, size_t tlen);

#endif /* WHATSNEW_H */
