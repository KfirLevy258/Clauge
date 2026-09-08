"""Round-trip tests for the BLGO company-logo builder.

Run: .venv-test/bin/python -m pytest tests/logo
Synthetic pictures only -- no ffmpeg, no real logo.
"""
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import encode_logo as lg  # noqa: E402
import encode_bootanim as ba  # noqa: E402


def picture(w=100, h=60, bg=(10, 20, 30)):
    im = Image.new("RGB", (w, h), bg)
    px = im.load()
    for y in range(h // 3, 2 * h // 3):
        for x in range(w // 4, 3 * w // 4):
            px[x, y] = (250, 60, 200)
    return im


class TestHeader(unittest.TestCase):
    def test_layout_matches_logo_parse_h(self):
        frames = [ba.to_rgb565(lg.fit_canvas(picture(), (10, 20, 30)))]
        img = lg.build(frames, 1, 3000, (10, 20, 30))
        self.assertEqual(img[:4], b"BLGO")
        version, hold, blob_len, crc = struct.unpack_from("<HHII", img, 4)
        self.assertEqual(version, 1)
        self.assertEqual(hold, 3000)
        self.assertEqual(tuple(img[16:19]), (10, 20, 30))
        self.assertEqual(img[19:32], b"\0" * 13)
        blob = img[32:32 + blob_len]
        self.assertEqual(blob[:4], b"BAN1")
        self.assertEqual(zlib.crc32(blob) & 0xffffffff, crc)
        self.assertEqual(len(img) % lg.ALIGN, 0)
        self.assertTrue(all(b == 0xff for b in img[32 + blob_len:]))

    def test_parse_rejects_what_the_firmware_rejects(self):
        frames = [ba.to_rgb565(lg.fit_canvas(picture(), (0, 0, 0)))]
        img = bytearray(lg.build(frames, 1, 100, (0, 0, 0)))
        lg.parse(bytes(img))
        bad = bytearray(img)
        bad[40] ^= 1
        with self.assertRaises(ValueError):
            lg.parse(bytes(bad))
        bad = bytearray(img)
        bad[4] = 2
        with self.assertRaises(ValueError):
            lg.parse(bytes(bad))
        with self.assertRaises(ValueError):
            lg.parse(b"\xff" * 4096)


class TestStill(unittest.TestCase):
    def test_fit_centres_and_letterboxes(self):
        canvas = lg.fit_canvas(picture(100, 60), (1, 2, 3))
        self.assertEqual(canvas.shape, (240, 320, 3))
        # 100x60 scaled to fit 320x240 -> 320x192, 24-row bars top/bottom.
        self.assertTrue((canvas[:24] == (1, 2, 3)).all())
        self.assertTrue((canvas[-24:] == (1, 2, 3)).all())
        self.assertFalse((canvas[100, 160] == (1, 2, 3)).all())

    def test_scale_shrinks(self):
        canvas = lg.fit_canvas(picture(320, 240), (0, 0, 0), scale=0.5)
        # Everything outside the centre 160x120 is background.
        self.assertTrue((canvas[:60] == 0).all())
        self.assertTrue((canvas[:, :80] == 0).all())

    def test_roundtrip_one_frame(self):
        canvas = lg.fit_canvas(picture(), (10, 20, 30))
        frames = lg.quantise(canvas[None], 10)
        img = lg.build(frames, 1, 3000, (10, 20, 30))
        info = lg.parse(img)
        self.assertEqual(len(info["frames"]), 1)
        self.assertTrue((info["frames"][0] == frames[0]).all())
        self.assertEqual(info["hold_ms"], 3000)
        # A flat picture is small (the resampled edges are the only
        # literals): the whole thing well under 8 KB.
        self.assertLess(len(img), 8192)

    def test_bg_parsing(self):
        self.assertEqual(lg.parse_bg("070b1e"), (7, 11, 30))
        self.assertEqual(lg.parse_bg("#070b1e"), (7, 11, 30))
        self.assertEqual(lg.parse_bg("7,11,30"), (7, 11, 30))
        self.assertEqual(lg.parse_bg("auto", picture(bg=(9, 8, 7))), (9, 8, 7))
        transparent = Image.new("RGBA", (4, 4), (255, 255, 255, 0))
        self.assertEqual(lg.parse_bg("auto", transparent), (0, 0, 0))


class TestClip(unittest.TestCase):
    def test_frames_dir_roundtrip_and_delta(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(4):
                im = Image.new("RGB", (320, 240), (5, 5, 5))
                px = im.load()
                for y in range(100, 140):
                    for x in range(40 + 20 * i, 80 + 20 * i):
                        px[x, y] = (200, 200, 0)
                im.save(os.path.join(d, f"{i:03d}.png"))
            canvases = lg.load_frames_dir(d)
        frames = lg.quantise(canvases, 0)
        img = lg.build(frames, 15, 500, (5, 5, 5))
        info = lg.parse(img)
        self.assertEqual(len(info["frames"]), 4)
        self.assertEqual(info["fps"], 15)
        for a, b in zip(info["frames"], frames):
            self.assertTrue((a == b).all())
        # Frame 0 is the whole canvas; the moving square costs little after.
        blob = img[32:32 + info["blob_len"]]
        self.assertLess(len(blob), 3000)


class TestCli(unittest.TestCase):
    def test_image_to_bin_and_info(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "logo.png")
            picture(200, 120, bg=(0, 0, 0)).save(src)
            out = os.path.join(d, "logo.bin")
            gif = os.path.join(d, "logo.gif")
            r = subprocess.run(
                [sys.executable, str(ROOT / "tools/encode_logo.py"),
                 "--image", src, "--hold", "2", "--out", out,
                 "--preview", gif],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("1 frame(s)", r.stdout)
            self.assertTrue(os.path.exists(gif))
            r = subprocess.run(
                [sys.executable, str(ROOT / "tools/encode_logo.py"),
                 "--info", out], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("hold 2000 ms", r.stdout)

    def test_hold_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "logo.png")
            picture().save(src)
            r = subprocess.run(
                [sys.executable, str(ROOT / "tools/encode_logo.py"),
                 "--image", src, "--hold", "61", "--out",
                 os.path.join(d, "x.bin")], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
