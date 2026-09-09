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
 * own header line on top of this. */
#define WHATSNEW_MAX 224

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
