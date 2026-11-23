"""Regression tests for destructive-edge cases and install configuration."""
import errno
import importlib.util
import os
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import media_archive as ma

spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SafetyRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "state"
        self.patcher = mock.patch.multiple(ma, APP_DIR=self.home, SETTINGS=self.home / "settings.json", DB_PATH=self.home / "history.sqlite3")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.target = self.root / "target"
        self.target.mkdir()
        ma.configure(str(self.target))
        self.cfg = ma.read_settings()
        self.db = ma.db_connect()
        self.addCleanup(self.db.close)
        ma.index_target(self.db, self.target, self.cfg)
        self.source = self.root / "source"
        self.source.mkdir()

    def test_unchanged_size_and_mtime_but_changed_bytes_is_not_duplicate(self):
        src = self.source / "frame.JPG"
        src.write_bytes(b"good")
        self.assertEqual(ma.import_one(self.db, self.cfg, self.target, src)[0], "imported")
        original = self.target / "图片" / src.name
        before = original.stat()
        original.write_bytes(b"evil")
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(ma.import_one(self.db, self.cfg, self.target, src)[0], "imported")
        self.assertEqual(len(list((self.target / "图片").glob("*.JPG"))), 2)
        self.assertEqual(original.read_bytes(), b"evil")

    def test_non_atomic_fallback_does_not_overwrite(self):
        src = self.source / "raw.ARW"
        src.write_bytes(b"binary")
        out = self.target / "raw.ARW"
        with mock.patch.object(ma.os, "link", side_effect=OSError(errno.EPERM, "hardlink unavailable")):
            ma.copy_verified(src, out, ma.sha256_file(src), 6)
        self.assertEqual(out.read_bytes(), b"binary")

    def test_fallback_collision_preserves_other_file(self):
        src = self.source / "raw.ARW"
        src.write_bytes(b"binary")
        out = self.target / "raw.ARW"
        original_open = ma.os.open
        def racing_open(path, flags, mode=0o777, **kw):
            if Path(path) == out:
                out.write_bytes(b"preexisting")
                raise FileExistsError("raced")
            return original_open(path, flags, mode, **kw)
        with mock.patch.object(ma.os, "link", side_effect=OSError(errno.EPERM, "unsupported")), mock.patch.object(ma.os, "open", side_effect=racing_open):
            with self.assertRaises(FileExistsError):
                ma.copy_verified(src, out, ma.sha256_file(src), 6)
        self.assertEqual(out.read_bytes(), b"preexisting")

    def test_destination_category_symlink_is_rejected_before_writes(self):
        external = self.root / "other"
        external.mkdir()
        (self.target / "视频").symlink_to(external, target_is_directory=True)
        src = self.source / "movie.mp4"
        src.write_bytes(b"movie")
        with mock.patch.object(ma, "video_day", return_value=("2026-11-22", "exiftool")):
            with self.assertRaises(ma.ImportErrorSafe):
                ma.import_one(self.db, self.cfg, self.target, src)
        self.assertEqual(list(external.iterdir()), [])

    def test_fallback_verification_mismatch_cleans_final_file(self):
        src = self.source / "input.jpg"
        src.write_bytes(b"original")
        dst = self.target / "input.jpg"
        real_hash = ma.sha256_file
        def fake_hash(p):
            if p == dst:
                return "0" * 64
            return real_hash(p)
        with mock.patch.object(ma.os, "link", side_effect=OSError(errno.EPERM, "no links")), mock.patch.object(ma, "sha256_file", side_effect=fake_hash):
            with self.assertRaises(ma.ImportErrorSafe):
                ma.copy_verified(src, dst, real_hash(src), 8)
        self.assertFalse(dst.exists())
        self.assertFalse(list(self.target.glob(".media-archive-partial-*")))

    def test_hidden_symlink_path_not_treated_as_free(self):
        victim = self.root / "victim"
        victim.write_bytes(b"untouched")
        (self.target / "sample.JPG").symlink_to(victim)
        candidate = ma.nonconflicting_path(self.target, "sample.JPG", "abcdef" * 10 + "abcd")
        self.assertNotEqual(candidate.name, "sample.JPG")
        self.assertEqual(victim.read_bytes(), b"untouched")

    def test_target_marker_rejected_when_missing(self):
        (self.target / ma.MARKER_NAME).unlink()
        with self.assertRaises(ma.ImportErrorSafe):
            ma.checked_target(self.cfg)

    def test_plist_contract_and_round_trip(self):
        cfg = installer.agent_plist(Path("/usr/bin/python3"), Path("/Users/test/MH/main.py"), self.home)
        roundtripped = plistlib.loads(plistlib.dumps(cfg))
        self.assertTrue(roundtripped["StartOnMount"])
        self.assertTrue(roundtripped["RunAtLoad"])
        self.assertEqual(roundtripped["ProgramArguments"][-1], "scan")
        self.assertEqual(roundtripped["Label"], installer.LABEL)


if __name__ == "__main__":
    unittest.main()