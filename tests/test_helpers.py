"""Unit tests for PhotoSorter helpers."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sorter import (
    UNSORTED_DIR_NAME,
    PhotoSorter,
    build_result,
    device_folder_name,
    device_from_make_model,
    parse_exif_datetime,
    parse_video_datetime,
    paths_overlap,
    read_jpeg_exif_bytes,
    safe_name,
    unique_path,
)


class HelperTests(unittest.TestCase):
    def test_device_from_make_model(self) -> None:
        self.assertEqual(
            device_from_make_model("Apple", "iPhone 12"),
            "Apple iPhone 12",
        )
        self.assertEqual(
            device_from_make_model("Canon", "Canon EOS 5D"),
            "Canon EOS 5D",
        )
        self.assertEqual(device_from_make_model("Sony", None), "Sony")
        self.assertEqual(device_from_make_model(None, "Pixel 7"), "Pixel 7")
        self.assertIsNone(device_from_make_model(None, None))
        self.assertIsNone(device_from_make_model("  ", ""))

    def test_parse_exif_datetime(self) -> None:
        dt = parse_exif_datetime("2020:01:02 03:04:05")
        self.assertEqual(dt, datetime(2020, 1, 2, 3, 4, 5))
        self.assertEqual(
            parse_exif_datetime(b"2020:01:02 03:04:05"),
            datetime(2020, 1, 2, 3, 4, 5),
        )
        self.assertIsNone(parse_exif_datetime("not-a-date"))
        self.assertIsNone(parse_exif_datetime(None))

    def test_parse_video_datetime(self) -> None:
        dt = parse_video_datetime("2020-01-02T03:04:05Z")
        self.assertIsNotNone(dt)
        assert dt is not None
        self.assertIsNone(dt.tzinfo)
        self.assertEqual(dt.year, 2020)
        self.assertEqual(
            parse_video_datetime("2020-01-02 03:04:05"),
            datetime(2020, 1, 2, 3, 4, 5),
        )

    def test_safe_name_and_device_folder(self) -> None:
        self.assertEqual(safe_name('a/b:c*?'), "a_b_c__")
        self.assertEqual(safe_name("   "), "Unknown")
        self.assertEqual(device_folder_name(None), UNSORTED_DIR_NAME)
        self.assertEqual(device_folder_name("  "), UNSORTED_DIR_NAME)
        self.assertEqual(device_folder_name("Pixel 7"), "Pixel 7")

    def test_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "photo.jpg"
            target.write_bytes(b"1")
            alt = unique_path(target)
            self.assertEqual(alt.name, "photo_1.jpg")
            alt.write_bytes(b"2")
            alt2 = unique_path(target)
            self.assertEqual(alt2.name, "photo_2.jpg")

    def test_paths_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dst = root / "dst"
            nested = src / "nested"
            src.mkdir()
            dst.mkdir()
            nested.mkdir()
            self.assertFalse(paths_overlap(src, dst))
            self.assertTrue(paths_overlap(src, src))
            self.assertTrue(paths_overlap(src, nested))
            self.assertTrue(paths_overlap(nested, src))

    def test_paths_overlap_fail_closed_on_oserror(self) -> None:
        with mock.patch.object(Path, "resolve", side_effect=OSError("boom")):
            self.assertTrue(paths_overlap(Path("/a"), Path("/b")))

    def test_build_result_uses_mtime_for_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.JPG"
            path.write_bytes(b"x")
            capture = datetime(2019, 5, 6, 7, 8, 9)
            result = build_result(path, capture, "Pixel 7")
            self.assertEqual(result["year"], "2019")
            self.assertEqual(result["device"], "Pixel 7")
            self.assertTrue(result["filename"].endswith(".jpg"))
            # filename comes from mtime, not capture date
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            self.assertTrue(
                result["filename"].startswith(mtime.strftime("%Y-%m-%d_%H-%M-%S"))
            )

    def test_read_jpeg_exif_bytes_non_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            self.assertIsNone(read_jpeg_exif_bytes(path))

    def test_copy_fallback_preserves_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "a.jpg"
            dest = root / "out"
            src.write_bytes(b"jpeg-bytes")
            sorter = PhotoSorter()
            out = sorter.copy_fallback(src, dest, year="2024", device="Pixel 7")
            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(out.parent.name, "Pixel 7")
            self.assertEqual(out.parent.parent.name, "2024")

    def test_cancel_stops_sort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            dest.mkdir()
            for i in range(3):
                (src / f"f{i}.jpg").write_bytes(b"\xff\xd8\xff\xd9")

            cancel = {"flag": False}

            def check() -> bool:
                return cancel["flag"]

            calls = {"n": 0}

            def on_progress(p: int, t: int, e: int) -> None:
                calls["n"] += 1
                if p >= 1:
                    cancel["flag"] = True

            sorter = PhotoSorter(on_progress=on_progress, cancel_check=check)
            success, processed, total, errors = sorter.sort_files(str(src), str(dest))
            self.assertFalse(success)
            self.assertEqual(total, 3)
            self.assertLess(processed, total)
            self.assertEqual(errors, 0)


if __name__ == "__main__":
    unittest.main()
