/* What the popup after an update says, pinned.
 *
 *   cc -Wall -Werror -I firmware/src tests/whatsnew/host_test.c \
 *      firmware/src/whatsnew.c firmware/src/ota_parse.c -o /tmp/whatsnew
 *
 * The popup used to say "Updated to version 1.3.2." and nothing else. The
 * customer who prompted this went 1.2.5 -> 1.3.2 in one tap -- three
 * releases -- so the interesting cases here are the multi-release walk and
 * the screen budget that stops it. See whatsnew.h.
 */
#include <stdio.h>
#include <string.h>
#include "whatsnew.h"

static int fails;

#define CHECK(c) do { \
	if (!(c)) { \
		fails++; \
		printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); \
	} else { \
		printf("PASS %s:%d %s\n", __FILE__, __LINE__, #c); \
	} \
} while (0)

/* Does `hay` contain `needle`? */
static int has(const char *hay, const char *needle)
{
	return strstr(hay, needle) != NULL;
}

/* How many lines the text would occupy before wrapping. */
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

int main(void)
{
	char buf[WHATSNEW_MAX];
	char from[16], to[16], trail[16];

	/* ---------------- the table is present and reachable ------------- */
	CHECK(whatsnew_has("1.3.2"));
	CHECK(whatsnew_has("1.3.0"));
	/* A release with no entry must be answerable, because release.sh
	 * asks exactly this before it will publish. */
	CHECK(!whatsnew_has("9.9.9"));
	CHECK(!whatsnew_has(""));

	/* ---------------- the panel's budget, asserted ------------------- */
	/*
	 * The box is 230 px tall with a 270 px label and an OK button under
	 * it: about eight lines of the default montserrat_14, at roughly 33
	 * characters before it wraps. Neither number is enforced anywhere in
	 * the firmware -- LVGL will happily lay a ninth line out underneath
	 * the button, where nobody sees it until a customer's board does it.
	 * So the budget is checked here, where adding a release that breaks
	 * it fails in CI instead of on a desk.
	 */
	for (int i = 0; i < whatsnew_entries(); i++) {
		const char *p = whatsnew_lines_at(i);

		CHECK(p[0] != '\0');
		CHECK(p[strlen(p) - 1] != '\n');   /* no trailing blank line */
		for (const char *e; ; p = e + 1) {
			e = strchr(p, '\n');
			/* One wrapped row per line, so the row count below is
			 * the line count and not a guess. */
			CHECK((size_t)(e ? e - p : (long)strlen(p)) <= 33);
			if (!e) {
				break;
			}
		}
	}
	/* The worst case that can reach the screen: every release in the
	 * table, plus the header the caller puts above it. */
	CHECK(whatsnew_render("0.0.1", "1.3.2", buf, sizeof(buf)) ==
	      whatsnew_entries());
	CHECK(lines(buf) + 1 <= 8);

	/* ---------------- one release at a time -------------------------- */
	CHECK(whatsnew_render("1.3.1", "1.3.2", buf, sizeof(buf)) == 1);
	CHECK(has(buf, "Updates report honestly"));
	/* Nothing from a release the board already had. */
	CHECK(!has(buf, "Claude Desktop"));

	/* ---------------- the reported jump: 1.2.5 -> 1.3.2 -------------- */
	CHECK(whatsnew_render("1.2.5", "1.3.2", buf, sizeof(buf)) == 3);
	CHECK(has(buf, "Updates report honestly"));       /* 1.3.2 */
	CHECK(has(buf, "out-of-date app"));               /* 1.3.1 */
	CHECK(has(buf, "Claude Desktop countdowns"));     /* 1.3.0 */
	/* It has to FIT: eight lines is the whole box, and the caller puts a
	 * header above this. */
	CHECK(lines(buf) <= 7);
	CHECK(strlen(buf) < WHATSNEW_MAX);

	/* ---------------- an unknown origin is not an error -------------- */
	/* A board updated by a release that recorded only its target reports
	 * "" here. Showing the one release we know it is running is the
	 * honest answer; showing nothing would be a regression on today. */
	CHECK(whatsnew_render("", "1.3.2", buf, sizeof(buf)) == 1);
	CHECK(has(buf, "Updates report honestly"));
	CHECK(!has(buf, "Claude Desktop"));
	CHECK(whatsnew_render("not-a-version", "1.3.2", buf, sizeof(buf)) == 1);

	/* ---------------- a version we have no entry for ----------------- */
	/* Say nothing rather than guess: the caller's header still names it. */
	CHECK(whatsnew_render("1.3.0", "9.9.9", buf, sizeof(buf)) == 0);
	CHECK(buf[0] == '\0');

	/* ---------------- nothing is invented about older releases ------- */
	/* There were no releases between 1.2.5 and 1.3.0, so a jump from
	 * further back than the table still must not claim there were. */
	CHECK(whatsnew_render("1.0.0", "1.3.2", buf, sizeof(buf)) == 3);
	CHECK(!has(buf, "earlier"));

	/* ---------------- the budget stops the walk cleanly -------------- */
	{
		char mid[96];

		/* Room for one release and the count, which is what the
		 * reservation exists to guarantee: without it the count was
		 * the first thing squeezed out, leaving a popup that silently
		 * claimed the newest release was all that changed. */
		CHECK(whatsnew_render("1.2.5", "1.3.2", mid, sizeof(mid)) == 1);
		CHECK(has(mid, "Updates report honestly"));
		CHECK(has(mid, "and 2 earlier updates."));
		CHECK(strlen(mid) < sizeof(mid));
	}
	{
		char small[64];

		/* Too small for both. The WORDS win: "and 2 earlier updates."
		 * on its own tells a customer nothing they can use, where one
		 * real improvement does. */
		CHECK(whatsnew_render("1.2.5", "1.3.2", small,
				      sizeof(small)) == 1);
		CHECK(has(small, "Updates report honestly"));
		/* Whole sentences or none -- never a clipped one. */
		CHECK(strlen(small) < sizeof(small));
	}
	{
		/* Absurdly small: no line fits at all. Must not write past
		 * the end, and must not leave a stray leading newline. */
		char tiny[24];

		whatsnew_render("1.2.5", "1.3.2", tiny, sizeof(tiny));
		CHECK(strlen(tiny) < sizeof(tiny));
		CHECK(tiny[0] != '\n');
	}
	{
		char one[2];

		CHECK(whatsnew_render("1.2.5", "1.3.2", one, sizeof(one)) == 0);
		CHECK(one[0] == '\0');
	}

	/* ---------------- the breadcrumb ---------------------------------- */
	/* 16 is CFG_OTA_VER_MAX, the real field this has to live in. */
	whatsnew_trail("1.2.5", "1.3.2", trail, 16);
	CHECK(strcmp(trail, "1.2.5>1.3.2") == 0);
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	CHECK(strcmp(from, "1.2.5") == 0);
	CHECK(strcmp(to, "1.3.2") == 0);

	/* Two-digit parts still fit, which is the case the 16 bytes were
	 * checked against rather than assumed. */
	whatsnew_trail("10.10.0", "10.11.0", trail, 16);
	CHECK(strcmp(trail, "10.10.0>10.11.0") == 0);
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	CHECK(strcmp(to, "10.11.0") == 0);

	/* When the pair does not fit, the TARGET survives whole. A truncated
	 * version string would name a release nobody shipped, and main.c
	 * compares this against BLINK_FW_VERSION to decide whether the update
	 * landed -- so a clipped target reports a good update as a failure. */
	whatsnew_trail("10.10.10", "10.10.11", trail, 16);
	CHECK(strcmp(trail, "10.10.11") == 0);
	whatsnew_split(trail, from, sizeof(from), to, sizeof(to));
	CHECK(from[0] == '\0');
	CHECK(strcmp(to, "10.10.11") == 0);

	/* An unknown origin packs as the target alone. */
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

	/* And an empty one, which is what a cleared breadcrumb reads as. */
	whatsnew_split("", from, sizeof(from), to, sizeof(to));
	CHECK(from[0] == '\0');
	CHECK(to[0] == '\0');

	/* Round trip, for every pair the table knows about. */
	{
		static const char *const vs[] = {"1.3.0", "1.3.1", "1.3.2"};

		for (int i = 0; i < 3; i++) {
			for (int j = 0; j < 3; j++) {
				whatsnew_trail(vs[i], vs[j], trail, 16);
				whatsnew_split(trail, from, sizeof(from),
					       to, sizeof(to));
				CHECK(strcmp(to, vs[j]) == 0);
				CHECK(strcmp(from, vs[i]) == 0);
			}
		}
	}

	printf(fails ? "FAILED\n" : "ok\n");
	return fails ? 1 : 0;
}
