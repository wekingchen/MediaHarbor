import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_archive as ma


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.home = root / "app"
        self.target = root / "archive"
        self.source = root / "usb"
        self.target.mkdir()
        self.source.mkdir()
        self.old = (ma.APP_DIR, ma.SETTINGS, ma.DB_PATH)
        ma.APP_DIR = self.home
        ma.SETTINGS = self.home / "settings.json"
        ma.DB_PATH = self.home / "history.sqlite3"
        self.addCleanup(self.restore)
        ma.configure(str(self.target))
        self.cfg = ma.read_settings()
        self.con = ma.db_connect()
        self.addCleanup(self.con.close)
        ma.index_target(self.con, self.target, self.cfg)

    def restore(self):
        ma.APP_DIR, ma.SETTINGS, ma.DB_PATH = self.old

    def test_photo_flat_and_second_copy_skipped(self):
        photo = self.source / "A.JPG"
        photo.write_bytes(b"photo-1")
        self.assertEqual(ma.import_one(self.con, self.cfg, self.target, photo), ("imported", 7))
        self.assertEqual((self.target / "图片/A.JPG").read_bytes(), b"photo-1")
        self.assertEqual(ma.import_one(self.con, self.cfg, self.target, photo), ("skipped", 0))
        self.assertFalse((self.target / "图片/2026-11-24").exists())

    def test_same_name_different_content_never_overwrites(self):
        photo = self.source / "A.jpg"
        photo.write_bytes(b"first")
        ma.import_one(self.con, self.cfg, self.target, photo)
        photo.write_bytes(b"second")
        ma.import_one(self.con, self.cfg, self.target, photo)
        photos = sorted((self.target / "图片").glob("*.jpg"))
        self.assertEqual(len(photos), 2)
        self.assertEqual({f.read_bytes() for f in photos}, {b"first", b"second"})
        self.assertTrue(any("__" in p.name for p in photos))

    def test_different_name_same_content_skipped(self):
        first = self.source / "a.heic"
        second = self.source / "b.heic"
        first.write_bytes(b"same")
        second.write_bytes(b"same")
        ma.import_one(self.con, self.cfg, self.target, first)
        self.assertEqual(ma.import_one(self.con, self.cfg, self.target, second), ("skipped", 0))
        self.assertEqual(len(list((self.target / "图片").iterdir())), 1)

    def test_video_day(self):
        video = self.source / "vid.MOV"
        video.write_bytes(b"video")
        with mock.patch.object(ma, "video_day", return_value=("2026-11-23", "exiftool")):
            ma.import_one(self.con, self.cfg, self.target, video)
        self.assertTrue((self.target / "视频/2026-11-23/vid.MOV").exists())

    def test_missing_target_marker_rejected(self):
        (self.target / ma.MARKER_NAME).unlink()
        with self.assertRaises(ma.ImportErrorSafe):
            ma.checked_target(self.cfg)

    def test_replacing_target_marker_rejected(self):
        (self.target / ma.MARKER_NAME).write_text(json.dumps({"target_id": "different"}))
        with self.assertRaises(ma.ImportErrorSafe):
            ma.checked_target(self.cfg)

    def test_preexisting_content_is_indexed(self):
        # Simulate picking a nonempty existing archive folder.
        target2 = Path(self.tmp.name) / "existing"
        (target2 / "图片").mkdir(parents=True)
        (target2 / "图片/old.PNG").write_bytes(b"already-here")
        ma.configure(str(target2))
        cfg2 = ma.read_settings()
        ma.index_target(self.con, target2, cfg2)
        copy = self.source / "renamed.PNG"
        copy.write_bytes(b"already-here")
        self.assertEqual(ma.import_one(self.con, cfg2, target2, copy), ("skipped", 0))
        self.assertEqual(len(list((target2 / "图片").iterdir())), 1)

    def test_deleted_copy_is_reimported(self):
        photo = self.source / "IMG.jpg"
        photo.write_bytes(b"abc")
        ma.import_one(self.con, self.cfg, self.target, photo)
        (self.target / "图片/IMG.jpg").unlink()
        self.assertEqual(ma.import_one(self.con, self.cfg, self.target, photo), ("imported", 3))

    def test_corrupt_destination_reimport_renamed(self):
        photo = self.source / "IMG.jpg"
        photo.write_bytes(b"good")
        ma.import_one(self.con, self.cfg, self.target, photo)
        (self.target / "图片/IMG.jpg").write_bytes(b"bad!")
        self.assertEqual(ma.import_one(self.con, self.cfg, self.target, photo), ("imported", 4))
        contents = {p.read_bytes() for p in (self.target / "图片").iterdir()}
        self.assertEqual(contents, {b"bad!", b"good"})

    def test_failed_copy_does_not_leave_partial(self):
        photo = self.source / "IMG.jpg"
        photo.write_bytes(b"abc")
        dst = self.target / "IMG.jpg"
        with self.assertRaises(ma.ImportErrorSafe):
            ma.copy_verified(photo, dst, "0" * 64, 3)
        self.assertFalse(dst.exists())
        self.assertFalse(any(self.target.glob(".media-archive-partial-*")))

    def test_source_and_target_same_volume_rejected(self):
        with self.assertRaises(ma.ImportErrorSafe):
            ma.import_source(self.con, self.cfg, self.target, self.source)

    def test_full_import_synthetic_other_disk(self):
        # In CI we emulate an SD card's different st_dev; all real file IO stays local.
        image = self.source / "folder1"
        image.mkdir()
        (image / "a.JPG").write_bytes(b"a")
        (image / "b.HEIC").write_bytes(b"b")
        (self.source / "ignore.txt").write_bytes(b"skip")
        actual_stat = Path.stat
        def simulate_dev(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if Path(path) == self.source:
                import types
                d = {name: getattr(result, name) for name in ("st_mode", "st_dev", "st_ino", "st_nlink", "st_uid", "st_gid", "st_size", "st_atime", "st_mtime", "st_ctime", "st_atime_ns", "st_mtime_ns", "st_ctime_ns")}
                d["st_dev"] = result.st_dev + 999
                return types.SimpleNamespace(**d)
            return result
        with mock.patch.object(Path, "stat", simulate_dev):
            first = ma.import_source(self.con, self.cfg, self.target, self.source)
            second = ma.import_source(self.con, self.cfg, self.target, self.source)
        self.assertEqual((first["imported"], first["skipped"], first["failed"]), (2, 0, 0))
        self.assertEqual((second["imported"], second["skipped"], second["failed"]), (0, 2, 0))
        self.assertEqual((image / "a.JPG").read_bytes(), b"a")  # SD/USB source never modified


    def test_cleanup_crash_leftover(self):
        video = self.target / "视频/2026-11-23"
        video.mkdir(parents=True)
        temp = video / ".media-archive-partial-abc"
        temp.write_bytes(b"partially written")
        actual = video / "good.MOV"
        actual.write_bytes(b"keep")
        ma.cleanup_partials(self.target)
        self.assertFalse(temp.exists())
        self.assertEqual(actual.read_bytes(), b"keep")

    def test_switch_target_has_independent_dedup(self):
        photo = self.source / "A.JPG"
        photo.write_bytes(b"shared")
        ma.import_one(self.con, self.cfg, self.target, photo)
        target2 = Path(self.tmp.name) / "second-disk"
        target2.mkdir()
        ma.configure(str(target2))
        cfg2 = ma.read_settings()
        ma.index_target(self.con, target2, cfg2)
        self.assertEqual(ma.import_one(self.con, cfg2, target2, photo), ("imported", 6))
        self.assertEqual(ma.import_one(self.con, cfg2, target2, photo), ("skipped", 0))
        self.assertTrue((self.target / "图片/A.JPG").exists())
        self.assertTrue((target2 / "图片/A.JPG").exists())

    def test_source_symlink_is_not_imported(self):
        actual = Path(self.tmp.name) / "secret.JPG"
        actual.write_bytes(b"secret")
        (self.source / "linked.JPG").symlink_to(actual)
        self.assertEqual(list(ma.walk_media(self.source)), [])

    def test_allowed_source_filters_wrong_volume(self):
        with mock.patch.object(ma, "volume_plist", return_value={"RemovableMedia": True}), mock.patch.object(ma.os.path, "ismount", return_value=True):
            # Same device is rejected, even if diskutil classifies it as removable.
            self.assertFalse(ma.allowed_source(self.source, self.cfg, self.target))

    def test_source_whitelist(self):
        ma.add_source("CAMERA_CARD")
        self.assertIn("CAMERA_CARD", ma.read_settings()["allowed_volume_names"])
        ma.add_source("CAMERA_CARD", remove=True)
        self.assertEqual(ma.read_settings()["allowed_volume_names"], [])

    def test_date_parser(self):
        self.assertEqual(ma.valid_date("2026:11:22 09:00:00"), "2026-11-22")
        self.assertIsNone(ma.valid_date("0000:00:00 00:00:00"))
        self.assertIsNone(ma.valid_date("not a date"))


if __name__ == "__main__":
    unittest.main(verbosity=2)