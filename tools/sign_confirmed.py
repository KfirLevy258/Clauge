#!/usr/bin/env python3
"""Sign a CONFIRMED copy of the app image, for direct flashing over USB.

    tools/sign_confirmed.py --build-dir firmware/build-sb --out zephyr.confirmed.bin

The build produces zephyr.signed.bin, which is signed but NOT confirmed: its
MCUboot trailer says "test this image once". That is exactly right for the OTA
artifact -- it lands in slot 1, boots once, and firmware/src/main.c calls
boot_write_img_confirmed() after the image proves itself (see ota_boot_pump).

It is exactly WRONG for a direct flash. A board written over USB has no daemon
and no network at the bench, so it never becomes healthy, and at 90 s
ota_boot_pump reboots to revert. On a blank unit that is invisible -- there is
nothing in the other slot to revert TO. On a board that already has firmware it
silently undoes the burn, minutes after the burn said PASS, and it looks for
all the world like a crash loop.

So: re-sign the same zephyr.bin with --pad --confirm, and flash that.

The signing parameters (version, header size, slot size, alignment, key) are
read out of the build's own build.ninja rather than written down here. They
follow the partition layout and the release version, and a second copy of them
is a copy that goes stale without saying so.
"""
import argparse
import os
import re
import subprocess
import sys

# Ninja escapes a literal space inside a command as "\ ". Split on whitespace
# that is not escaped, then unescape -- a build directory with a space in its
# path is otherwise silently torn in half.
_UNESCAPED_WS = re.compile(r"(?<!\\)\s+")


def split_ninja(segment):
	return [t.replace("\\ ", " ") for t in _UNESCAPED_WS.split(segment.strip()) if t]


def find_ninja(build_dir):
	"""The app image's build.ninja: sysbuild nests it under firmware/."""
	for rel in ("firmware/build.ninja", "build.ninja"):
		path = os.path.join(build_dir, rel)
		if os.path.isfile(path):
			return path
	return None


def imgtool_argv(ninja_path):
	"""The `imgtool.py sign` call the build ran, split into argv.

	Returns (argv-up-to-and-including-"sign" plus its options, infile,
	outfile). Raises ValueError carrying a message meant for whoever is
	standing at the bench.
	"""
	with open(ninja_path, "r", encoding="utf-8", errors="replace") as fh:
		text = fh.read()

	# Tokenise, then look for the `<something>/imgtool.py sign` pair, rather
	# than doing arithmetic on the substring "imgtool.py sign": that index
	# lands INSIDE the script's path, and backing up one space from it finds
	# the path itself, not the interpreter in front of it. Running imgtool.py
	# without its interpreter falls through to the shebang and the system
	# python3, which has no click -- so the failure is a stack trace from an
	# unrelated interpreter, not anything that names the real mistake.
	hits = []
	for line in text.splitlines():
		toks = split_ninja(line)
		for i, tok in enumerate(toks):
			if not tok.endswith("imgtool.py"):
				continue
			if i + 1 >= len(toks) or toks[i + 1] != "sign":
				continue
			# Include the interpreter unless imgtool is run directly.
			start = i - 1 if i > 0 and toks[i - 1] != "&&" else i
			seg = toks[start:]
			# Stop at the next chained command, so a longer POST_BUILD
			# chain cannot smuggle extra tokens into the argv.
			if "&&" in seg:
				seg = seg[:seg.index("&&")]
			hits.append(seg)

	if not hits:
		raise ValueError(
			"no `imgtool.py sign` in %s.\n"
			"       This build was made without MCUboot, so there is no image\n"
			"       to confirm. Build with -DSB_CONFIG_BOOTLOADER_MCUBOOT=y."
			% ninja_path)

	argv = hits[0]
	try:
		sign_at = argv.index("sign")
	except ValueError:
		raise ValueError("malformed imgtool call in %s" % ninja_path)

	# imgtool's sign options all take a value, so a token starting with "-"
	# swallows the token after it unless it is written --opt=value.
	opts, positional = [], []
	i = sign_at + 1
	while i < len(argv):
		tok = argv[i]
		if tok.startswith("-"):
			if "=" in tok:
				opts.append(tok)
				i += 1
			else:
				opts.extend(argv[i:i + 2])
				i += 2
		else:
			positional.append(tok)
			i += 1

	if len(positional) != 2:
		raise ValueError(
			"expected an infile and an outfile in the imgtool call in %s,"
			" found %r" % (ninja_path, positional))

	# --pad and --confirm are ours to add. Drop any the build already passed
	# so running this against such a build cannot produce "--confirm --confirm".
	opts = [o for o in opts if o not in ("--pad", "--confirm")]
	return argv[:sign_at + 1] + opts, positional[0], positional[1]


def sign(interp_argv, infile, outfile):
	proc = subprocess.run(interp_argv + ["--pad", "--confirm", infile, outfile],
			      capture_output=True, text=True)
	if proc.returncode != 0:
		sys.stderr.write(proc.stdout + proc.stderr)
		raise ValueError("imgtool could not sign a confirmed image")


def verify_confirmed(interp_argv, path):
	"""Ask imgtool what it just wrote: a confirmed trailer says image_ok SET.

	Checked rather than assumed, because this one byte is the whole point of
	the file. A --confirm that quietly stopped working puts the revert back,
	and its only symptom is a board that reverts 90 s after a burn said PASS.

	None means "this imgtool cannot tell us" -- not "fine".

	An image with no trailer at all prints no image_ok line and still exits
	0, which is what an unpadded zephyr.signed.bin does. That is a FAILURE,
	not an unknown: only a dumpinfo that would not run at all is unknown.
	"""
	runner = interp_argv[:interp_argv.index("sign")]
	proc = subprocess.run(runner + ["dumpinfo", path],
			      capture_output=True, text=True)
	if proc.returncode != 0:
		return None
	for line in proc.stdout.splitlines():
		if line.startswith("image_ok:"):
			return "SET" in line
	return False


def main(argv=None):
	ap = argparse.ArgumentParser(description="sign a confirmed image for direct flashing")
	ap.add_argument("--build-dir", required=True)
	ap.add_argument("--out", required=True)
	args = ap.parse_args(argv)

	ninja = find_ninja(args.build_dir)
	if ninja is None:
		sys.stderr.write(
			"FATAL: no build.ninja under %s -- nothing to read the signing\n"
			"       parameters from.\n" % args.build_dir)
		return 1

	try:
		interp_argv, infile, _ = imgtool_argv(ninja)
	except ValueError as exc:
		sys.stderr.write("FATAL: %s\n" % exc)
		return 1

	if not os.path.isfile(infile):
		sys.stderr.write("FATAL: no unsigned image at %s\n" % infile)
		return 1

	try:
		sign(interp_argv, infile, args.out)
	except ValueError as exc:
		sys.stderr.write("FATAL: %s\n" % exc)
		return 1

	# --pad fills the image out to the whole slot; the trailer lives in the
	# last bytes of it. A short file means --pad did not take, and then the
	# trailer is not where MCUboot looks for it.
	if "--slot-size" in interp_argv:
		slot = int(interp_argv[interp_argv.index("--slot-size") + 1], 0)
		size = os.path.getsize(args.out)
		if size != slot:
			sys.stderr.write(
				"FATAL: %s is %d bytes but the slot is %d. --pad did not take.\n"
				% (args.out, size, slot))
			return 1

	ok = verify_confirmed(interp_argv, args.out)
	if ok is False:
		sys.stderr.write(
			"FATAL: %s came back with image_ok unset. It would boot once and\n"
			"       revert 90 s later. Refusing to hand it to the flasher.\n"
			% args.out)
		return 1
	print(args.out)
	if ok is None:
		print("   (this imgtool has no dumpinfo -- trailer not verified)")
	return 0


if __name__ == "__main__":
	sys.exit(main())
