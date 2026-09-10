/* What the panel says after an update, pinned.
 *
 *   cc -Wall -Werror -I firmware/src tests/whatsnew/host_test.c \
 *      firmware/src/whatsnew.c firmware/src/ota_parse.c -o /tmp/whatsnew
 *
 * The popup used to say "Updated to version 1.3.2." and nothing else. The
 * customer who prompted this went 1.2.5 -> 1.3.2 in one tap -- three
 * releases -- so the interesting cases are the multi-release span, how it
 * packs into pages, and the breadcrumb that makes any of it knowable at all.
 * See whatsnew.h.
 */
#include <stdio.h>
#include <string.h>
#include "whatsnew.h"
#include "ota_parse.h"

static int fails;

#define CHECK(c) do { \
	if (!(c)) { \
		fails++; \
		printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); \
	} else { \
		printf("PASS %s:%d %s\n", __FILE__, __LINE__, #c); \
	} \
} while (0)

static int has(const char *hay, const char *needle)
{
	return strstr(hay, needle) != NULL;
}

static int lines(const char *s)
{
	int n = s[0] ? 1 : 0;

	for (; *s; s++) {
		if (*s == '\n') {
			n++;
		}
	}
	return n;
}

/*
 * How many releases lie between `from` and `to`.
 *
 * Computed here rather than assumed to be the whole table. It was assumed,
 * and that only held because the real table happens to contain exactly the
 * three releases of the reported jump -- so "the span" and "the table" were
 * the same number by coincidence. Loading a ten-release table to exercise
 * the pager separated them and 24 checks failed, all of them measuring the
 * wrong quantity. The assertion would have gone on passing until the fourth
 * release was added, and then failed for a reason nobody would have believed.
 */
static int span_of(const char *from, const char *to)
{
	int n = 0;
	int seen_to = 0;

	for (int i = 0; i < whatsnew_entries(); i++) {
		const char *v = whatsnew_version_at(i);

		if (!seen_to) {
			if (strcmp(v, to) != 0) {
				continue;
			}
			seen_to = 1;
		}
		if (n && from && from[0] && !ota_version_newer(v, from)) {
			break;
		}
		n++;
		if (!from || !from[0]) {
			break;      /* unknown origin: the landed release only */
		}
	}
	return n;
}

/* What a page costs, by the same arithmetic whatsnew.c uses. */
static int page_px(const struct whatsnew_page *p)
{
	int px = 0;

	for (int i = 0; i < p->count; i++) {
		px += WHATSNEW_VER_PX
		    + lines(whatsnew_lines_at(p->first + i)) * WHATSNEW_LINE_PX
		    + (i ? WHATSNEW_GAP_PX : 0);
	}
	return px;
}

int main(void)
{
	char buf[WHATSNEW_SUMMARY_MAX];
	char from[16], to[16], trail[16];
	struct whatsnew_page pages[16];

	/* ---------------- the table is present and reachable ------------- */
	CHECK(whatsnew_has("1.3.2"));
	CHECK(whatsnew_has("1.3.0"));
	/* release.sh asks exactly this before it will publish. */
	CHECK(!whatsnew_has("9.9.9"));
	CHECK(!whatsnew_has(""));
	CHECK(whatsnew_entries() >= 1);
	/*
	 * And the table has a ceiling, asserted here because nothing else
	 * would notice it being passed.
	 *
	 * The screen paginates into an array of WHATSNEW_MAX_PAGES on the
	 * stack. A page holds at least one release, so the page count can
	 * never exceed the entry count -- one number bounds both, and this is
	 * the check that makes the number real. The release that pushes the
	 * table past it fails here, at `tools/release.sh` time, rather than
	 * drawing a blank page on the desk of the one customer far enough
	 * behind to page that deep. Trim the oldest entries when it fires.
	 */
	CHECK(whatsnew_entries() <= WHATSNEW_MAX_PAGES);
	for (int i = 0; i < whatsnew_entries(); i++) {
		CHECK(whatsnew_version_at(i)[0] != '\0');
		CHECK(whatsnew_lines_at(i)[0] != '\0');
	}
	/* Out of range must answer rather than walk off the table: the screen
	 * asks for entries by index while paging. */
	CHECK(whatsnew_version_at(-1)[0] == '\0');
	CHECK(whatsnew_lines_at(whatsnew_entries())[0] == '\0');

	/* ---------------- the copy rules, checked not described ---------- */
	for (int i = 0; i < whatsnew_entries(); i++) {
		const char *p = whatsnew_lines_at(i);

		/* No entry may need a page to itself. whatsnew_paginate
		 * admits the first release on a page unconditionally -- that
		 * is what stops it looping -- so a release taller than a page
		 * would overrun one. */
		CHECK(lines(p) <= WHATSNEW_LINES_PER_ENTRY);
		CHECK(WHATSNEW_VER_PX + lines(p) * WHATSNEW_LINE_PX
		      <= WHATSNEW_PAGE_PX);
		CHECK(p[strlen(p) - 1] != '\n');   /* no trailing blank line */
		/* ~33 characters before the screen's 296 px wraps, and a
		 * wrapped line is a row this arithmetic did not count. */
		for (const char *e; ; p = e + 1) {
			e = strchr(p, '\n');
			CHECK((size_t)(e ? e - p : (long)strlen(p)) <= 33);
			if (!e) {
				break;
			}
		}
	}

	/* ---------------- the summary the notice shows ------------------- */
	CHECK(whatsnew_summary("1.2.5", "1.3.2", buf, sizeof(buf)) == 5);
	CHECK(has(buf, "5 changes since 1.2.5"));
	/* One release: "since" is dropped, because "2 changes since 1.3.1"
	 * on an update FROM 1.3.1 is a comparison nobody asked for. */
	CHECK(whatsnew_summary("1.3.1", "1.3.2", buf, sizeof(buf)) == 2);
	CHECK(strcmp(buf, "2 changes") == 0);
	/* An unknown origin names no version it is not entitled to name. */
	CHECK(whatsnew_summary("", "1.3.2", buf, sizeof(buf)) == 2);
	CHECK(!has(buf, "since"));
	/* A version with no entry says nothing at all, and the caller shows
	 * its title alone rather than an empty line under it. */
	CHECK(whatsnew_summary("1.3.0", "9.9.9", buf, sizeof(buf)) == 0);
	CHECK(buf[0] == '\0');
	/* Singular, because "1 changes" is the tell of a count nobody read. */
	CHECK(whatsnew_summary("1.3.0", "1.3.1", buf, sizeof(buf)) == 1);
	CHECK(strcmp(buf, "1 change") == 0);

	/* ---------------- pagination: the reported jump ------------------ */
	/* Three releases and five changes fit one page, so the customer who
	 * prompted all of this never sees a pager at all. */
	CHECK(span_of("1.2.5", "1.3.2") == 3);
	CHECK(whatsnew_paginate("1.2.5", "1.3.2", pages, 16) >= 1);
	CHECK(pages[0].first == 0);
	CHECK(page_px(&pages[0]) <= WHATSNEW_PAGE_PX);

	CHECK(whatsnew_paginate("1.3.1", "1.3.2", pages, 16) == 1);
	CHECK(pages[0].count == 1);
	/* An unknown origin yields the one release we know is running. */
	CHECK(whatsnew_paginate("", "1.3.2", pages, 16) == 1);
	CHECK(pages[0].count == 1);
	CHECK(whatsnew_paginate("1.3.0", "9.9.9", pages, 16) == 0);

	/* ---------------- pagination: the rules, at sizes we can build --- */
	/* Every page inside its budget, and no release ever split across a
	 * page turn -- so the counts have to add back up to the whole span. */
	for (int px = 40; px <= 200; px += 7) {
		int n = whatsnew_paginate_px("1.2.5", "1.3.2", pages, 16, px);
		int total = 0;

		CHECK(n >= 1);
		for (int i = 0; i < n && i < 16; i++) {
			CHECK(pages[i].count >= 1);
			/* A page holding more than one release must fit. One
			 * holding a single release is admitted whatever the
			 * budget, which is what stops this looping. */
			if (pages[i].count > 1) {
				CHECK(page_px(&pages[i]) <= px);
			}
			CHECK(pages[i].first == (i ? pages[i - 1].first
						 + pages[i - 1].count : 0));
			total += pages[i].count;
		}
		CHECK(total == span_of("1.2.5", "1.3.2"));
	}

	/* A budget too small for two releases gives one page each. */
	CHECK(whatsnew_paginate_px("1.2.5", "1.3.2", pages, 16, 50)
	      == span_of("1.2.5", "1.3.2"));
	for (int i = 0; i < span_of("1.2.5", "1.3.2") && i < 16; i++) {
		CHECK(pages[i].count == 1);
	}

	/* The TOTAL comes back even when it exceeds `max`, because the pager
	 * prints it -- truncating it silently would put "1 / 1" on a screen
	 * that has three pages. */
	{
		struct whatsnew_page one[1];

		CHECK(whatsnew_paginate_px("1.2.5", "1.3.2", one, 1, 50)
		      == span_of("1.2.5", "1.3.2"));
		CHECK(one[0].count == 1);
	}
	/* A NULL out is legal, for a caller that only wants the count. */
	CHECK(whatsnew_paginate_px("1.2.5", "1.3.2", NULL, 0, 50)
	      == span_of("1.2.5", "1.3.2"));

	/* ---------------- the breadcrumb ---------------------------------- */
	/* 16 is CFG_OTA_VER_MAX, the real field this has to live in. */
	whatsnew_trail("1.2.5", "1.3.2", trail, 16);
	CHECK(strcmp(trail, "1.2.5>1.3.2") == 0);
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	CHECK(strcmp(from, "1.2.5") == 0);
	CHECK(strcmp(to, "1.3.2") == 0);

	/* Two-digit parts still fit -- checked, not assumed. */
	whatsnew_trail("10.10.0", "10.11.0", trail, 16);
	CHECK(strcmp(trail, "10.10.0>10.11.0") == 0);
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	CHECK(strcmp(to, "10.11.0") == 0);

	/* When the pair does not fit, the TARGET survives whole. A truncated
	 * version names a release nobody shipped, and main.c compares this
	 * against BLINK_FW_VERSION to decide whether the update landed -- so
	 * a clipped target reports a good update as a failure. */
	whatsnew_trail("10.10.10", "10.10.11", trail, 16);
	CHECK(strcmp(trail, "10.10.11") == 0);
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	CHECK(from[0] == '\0');
	CHECK(strcmp(to, "10.10.11") == 0);

	whatsnew_trail("", "1.3.2", trail, 16);
	CHECK(strcmp(trail, "1.3.2") == 0);

	/* A breadcrumb from before any of this existed: no separator, so the
	 * whole string is the target and the origin is unknown. THIS is the
	 * compatibility case that matters -- every board in the field has one
	 * of these, and reading it as a `from` would announce every update as
	 * a failure. */
	whatsnew_split("1.3.2", from, sizeof(from), to, sizeof(to));
	CHECK(from[0] == '\0');
	CHECK(strcmp(to, "1.3.2") == 0);

	whatsnew_split("", from, sizeof(from), to, sizeof(to));
	CHECK(from[0] == '\0');
	CHECK(to[0] == '\0');

	/* Round trip, for every pair the table knows about. */
	for (int i = 0; i < whatsnew_entries(); i++) {
		for (int j = 0; j < whatsnew_entries(); j++) {
			whatsnew_trail(whatsnew_version_at(i),
				       whatsnew_version_at(j), trail, 16);
			whatsnew_split(trail, from, sizeof(from),
				       to, sizeof(to));
			CHECK(strcmp(to, whatsnew_version_at(j)) == 0);
			CHECK(strcmp(from, whatsnew_version_at(i)) == 0);
		}
	}

	printf(fails ? "FAILED\n" : "ok\n");
	return fails ? 1 : 0;
}
