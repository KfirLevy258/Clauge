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

/* Enough for the panel and no more -- see constraint 1. The caller adds its
 * own title above this. */
#define WHATSNEW_MAX 224

/*
 * And the limit that actually matters: ROWS, not bytes.
 *
 * A byte budget does not bound the height of anything. 224 bytes of short
 * lines is thirty rows, and the notice box is clamped to 230 px with
 * LV_OBJ_FLAG_SCROLLABLE cleared (ui_settings.c) -- so rows past the bottom
 * do not scroll and are not clipped. They push the OK button off the panel,
 * and a popup whose only dismiss button is off the panel is a board the
 * customer cannot get past.
 *
 * The arithmetic: 230 px of box, less 24 of padding, less a 20 px
 * montserrat_16 title, less two 12 px flex gaps, less the 36 px button,
 * leaves 126 px. At the default montserrat_14's ~17 px line that is 7 rows.
 *
 * FOUR, not seven, and not the six that also fits. Every number in that sum
 * is an estimate -- the 17 px line most of all -- and the cost of the
 * estimate being wrong is not a clipped row, it is the dismiss button off
 * the panel. Four spends about 68 px of the 126, so the line height can be a
 * third larger than assumed and nothing is lost. Six spends 102 and leaves
 * no room to be wrong.
 *
 * It is also the better read. This is a gauge glanced at from across a desk:
 * four rows is a glance and six is a paragraph. The release the customer
 * landed on keeps its detail, everything behind it becomes an honest count,
 * and the full notes are on the website for anyone who wants them.
 *
 * A hard cap, not a target. Unlike the byte budget, where overflowing costs
 * a truncated sentence, overflowing here costs the button -- so no entry is
 * ever admitted past it, not even the first one.
 */
#define WHATSNEW_ROWS 4

/* The most rows any single entry may occupy. Two, so that one release can
 * never fill the budget on its own and leave no room for the count line
 * behind it. Enforced by the host test rather than at runtime: it is a rule
 * about the copy someone writes at release time. */
#define WHATSNEW_ROWS_PER_ENTRY 2

/*
 * Fill `buf` with what changed, landing on `to` from `from`.
 *
 * `from` may be "" or unparseable, which is what a board updated by a
 * release that did not record where it came from reports. That is not an
 * error: it renders `to`'s own entry alone, which is the honest answer to
 * "what do we know".
 *
 * Returns the number of releases described. Zero means there is nothing to
 * say -- an unknown `to`, or a table with no entry for it -- and the caller
 * should show its header alone rather than an empty box.
 */
int whatsnew_render(const char *from, const char *to, char *buf, size_t len);

/* The same, with the row cap given explicitly. Exists so the host test can
 * prove the cap bites at a size it can construct, rather than only asserting
 * that today's three-entry table happens to fit. */
int whatsnew_render_rows(const char *from, const char *to, char *buf,
			 size_t len, int rows);

/* Is there an entry for this version? tools/release.sh asks, through the
 * host test, so that a release cannot ship without one. */
int whatsnew_has(const char *version);

/*
 * The table itself, for tests/whatsnew.
 *
 * Exposed so the panel's width and height budget can be ASSERTED rather than
 * written down in a comment above the table and then quietly outgrown by the
 * next release's entry. The failure it guards is not a crash: it is a line
 * that wraps onto a ninth row and disappears under the OK button, which
 * nobody sees until a customer's board does it.
 */
int whatsnew_entries(void);
const char *whatsnew_lines_at(int i);

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
