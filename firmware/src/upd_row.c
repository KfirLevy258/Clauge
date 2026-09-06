#include "upd_row.h"

enum upd_row upd_row_idle(bool badge, bool host_outdated)
{
	/* A downloaded update outranks the advisory: it is the one thing on
	 * this screen the customer can act on with a tap. */
	if (badge) {
		return UPD_ROW_READY;
	}
	if (host_outdated) {
		return UPD_ROW_APP_OLD;
	}
	return UPD_ROW_BLANK;
}

enum upd_row upd_row_checked(bool host_outdated)
{
	/* The check answers "is there newer FIRMWARE", and the answer here was
	 * no. That is not the same question as "is this product current", and
	 * reporting it as though it were is the bug this file exists for. */
	if (host_outdated) {
		return UPD_ROW_APP_OLD;
	}
	return UPD_ROW_UP_TO_DATE;
}
