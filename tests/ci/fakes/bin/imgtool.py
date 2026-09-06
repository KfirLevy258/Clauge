#!/usr/bin/env python3
"""Stand-in for mcuboot's imgtool.py (tests/ci/check_factory.sh).

Records every call to $FAKE_TOOL_LOG. `sign` copies the input, pads it to the
slot size, and writes a marker in the last bytes when --confirm was asked for;
`dumpinfo` reads that marker back and prints the one line tools/sign_confirmed.py
looks at. FAKE_IMGTOOL_NO_CONFIRM=1 makes --confirm quietly do nothing, which is
the failure this whole path exists to catch.
"""
import os
import sys

MARK = b"BLINK-FAKE-CONFIRMED"


def main(argv):
	with open(os.environ["FAKE_TOOL_LOG"], "a") as log:
		log.write("imgtool %s\n" % " ".join(argv))

	if not argv:
		return 2
	cmd, rest = argv[0], argv[1:]

	if cmd == "dumpinfo":
		with open(rest[0], "rb") as fh:
			fh.seek(-len(MARK), os.SEEK_END)
			tail = fh.read()
		# A real imgtool prints the trailer only when there is one, and
		# exits 0 either way.
		if tail == MARK:
			print("image_ok:    SET (0x1)")
		print("dumpinfo has run successfully")
		return 0

	if cmd != "sign":
		return 2

	slot, confirm, positional = 0, False, []
	i = 0
	while i < len(rest):
		tok = rest[i]
		if tok == "--slot-size":
			slot = int(rest[i + 1], 0)
			i += 2
		elif tok == "--confirm":
			confirm = True
			i += 1
		elif tok == "--pad":
			i += 1
		elif tok.startswith("-"):
			i += 2
		else:
			positional.append(tok)
			i += 1

	if len(positional) != 2:
		return 2
	with open(positional[0], "rb") as fh:
		data = fh.read()
	if slot:
		data = data[:slot].ljust(slot, b"\xff")
	if confirm and not os.environ.get("FAKE_IMGTOOL_NO_CONFIRM"):
		data = data[:-len(MARK)] + MARK
	with open(positional[1], "wb") as fh:
		fh.write(data)
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
