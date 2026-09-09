#include "upd_tap.h"

enum upd_act upd_tap_act(bool install_req, bool check_req, bool staged)
{
	if (install_req) {
		/* The queued check, if any, is deliberately dropped on the
		 * floor here rather than remembered for the next turn: see
		 * upd_tap.h. Consent is the whole of what this link carries. */
		(void)check_req;
		return staged ? UPD_ACT_INSTALL : UPD_ACT_REFUSED;
	}
	if (check_req) {
		return UPD_ACT_CHECK;
	}
	return UPD_ACT_NONE;
}
