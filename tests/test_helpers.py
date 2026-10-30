"""Unit tests for PhotoSorter helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

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
    reserve_unique_path,
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

    def test_reserve_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "photo.jpg"
            first = reserve_unique_path(target)
            self.assertEqual(first, target)
            self.assertTrue(first.exists())
            second = reserve_unique_path(target)
            self.assertEqual(second.name, "photo_1.jpg")
            self.assertTrue(second.exists())

    def test_cancel_stops_sort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            dest.mkdir()
            for i in range(20):
                (src / f"f{i}.jpg").write_bytes(b"\xff\xd8\xff\xd9")

            cancel = {"flag": False}

            def check() -> bool:
                return cancel["flag"]

            def on_progress(p: int, t: int, e: int, s: int = 0) -> None:
                if p >= 1:
                    cancel["flag"] = True

            sorter = PhotoSorter(
                on_progress=on_progress,
                cancel_check=check,
                workers=4,
            )
            success, processed, total, errors, skipped = sorter.sort_files(
                str(src), str(dest)
            )
            self.assertFalse(success)
            self.assertEqual(total, 20)
            self.assertLess(processed, total)
            self.assertEqual(errors, 0)
            self.assertEqual(skipped, 0)

    def test_parallel_unique_destination_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            dest.mkdir()
            shared_mtime = 1_700_000_000
            for i in range(8):
                path = src / f"f{i}.jpg"
                path.write_bytes(b"\xff\xd8\xff\xd9" + bytes([i]))
                os.utime(path, (shared_mtime, shared_mtime))

            sorter = PhotoSorter(workers=4)
            success, processed, total, errors, skipped = sorter.sort_files(
                str(src), str(dest)
            )
            self.assertTrue(success)
            self.assertEqual(processed, 8)
            self.assertEqual(total, 8)
            self.assertEqual(errors, 0)
            self.assertEqual(skipped, 0)

            copied = list(dest.rglob("*.jpg"))
            self.assertEqual(len(copied), 8)
            self.assertEqual(len({p.resolve() for p in copied}), 8)
            payloads = sorted(p.read_bytes() for p in copied)
            expected = sorted(b"\xff\xd8\xff\xd9" + bytes([i]) for i in range(8))
            self.assertEqual(payloads, expected)

    def test_avoid_duplicates_skips_identical_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            dest.mkdir()
            shared_mtime = 1_700_000_000
            for i in range(3):
                path = src / f"f{i}.jpg"
                path.write_bytes(b"identical-photo-content")
                os.utime(path, (shared_mtime, shared_mtime))

            def fake_meta(file_path: Path) -> tuple[dict, list]:
                return (
                    {
                        "datetime": datetime(2024, 1, 1, 12, 0, 0),
                        "device": "Pixel 7",
                        "year": "2024",
                        "filename": "2024-01-01_12-00-00.jpg",
                    },
                    [],
                )

            logs: list[str] = []

            with mock.patch("sorter.get_file_metadata", fake_meta):
                sorter = PhotoSorter(
                    workers=1,
                    avoid_duplicates=True,
                    on_log=lambda msg, _lvl: logs.append(msg),
                )
                success, processed, total, errors, skipped = sorter.sort_files(
                    str(src), str(dest)
                )

            self.assertTrue(success)
            self.assertEqual(total, 3)
            self.assertEqual(processed, 3)
            self.assertEqual(errors, 0)
            self.assertEqual(skipped, 2)
            copied = list(dest.rglob("*.jpg"))
            self.assertEqual(len(copied), 1)
            self.assertEqual(copied[0].name, "2024-01-01_12-00-00.jpg")
            self.assertEqual(copied[0].parent.name, "Pixel 7")
            self.assertTrue(any("Точный дубль" in msg for msg in logs))
            self.assertTrue(
                any(
                    msg.count(str(src)) == 2
                    for msg in logs
                    if "Точный дубль" in msg
                )
            )

    def test_exact_duplicate_of_existing_destination_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            existing = dest / "archive" / "existing.jpg"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"same-image-file")
            incoming = src / "incoming.jpg"
            incoming.write_bytes(existing.read_bytes())

            logs: list[str] = []
            sorter = PhotoSorter(
                workers=2,
                avoid_duplicates=True,
                on_log=lambda msg, _lvl: logs.append(msg),
            )
            success, processed, total, errors, skipped = sorter.sort_files(
                str(src), str(dest)
            )

            self.assertTrue(success)
            self.assertEqual((processed, total, errors, skipped), (1, 1, 0, 1))
            self.assertEqual(len(list(dest.rglob("*.jpg"))), 1)
            self.assertTrue(any("SHA-256" in msg for msg in logs))
            report = next(msg for msg in logs if "Точный дубль" in msg)
            self.assertIn(str(incoming), report)
            self.assertIn(str(existing), report)

    def test_different_contents_with_same_destination_name_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            for name, content in (
                ("first.jpg", b"first-photo"),
                ("second.jpg", b"second-photo"),
            ):
                path = src / name
                path.write_bytes(content)
                os.utime(path, (1_700_000_000, 1_700_000_000))

            def fake_meta(_file_path: Path) -> tuple[dict, list]:
                return (
                    {
                        "datetime": datetime(2024, 1, 1, 12, 0, 0),
                        "device": "Pixel 7",
                        "year": "2024",
                        "filename": "2024-01-01_12-00-00.jpg",
                    },
                    [],
                )

            with mock.patch("sorter.get_file_metadata", fake_meta):
                sorter = PhotoSorter(workers=1, avoid_duplicates=True)
                success, processed, total, errors, skipped = sorter.sort_files(
                    str(src), str(dest)
                )

            self.assertTrue(success)
            self.assertEqual((processed, total, errors, skipped), (2, 2, 0, 0))
            copied = sorted(dest.rglob("*.jpg"))
            self.assertEqual(len(copied), 2)
            self.assertEqual(
                {path.read_bytes() for path in copied},
                {b"first-photo", b"second-photo"},
            )

    def test_similar_photo_is_logged_and_still_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            shared_mtime = 1_700_000_000
            for name, quality in (("first.jpg", 95), ("second.jpg", 40)):
                image = Image.new("RGB", (128, 96), "white")
                draw = ImageDraw.Draw(image)
                draw.rectangle((12, 14, 70, 75), fill=(20, 80, 160))
                draw.ellipse((75, 20, 115, 60), fill=(220, 40, 30))
                path = src / name
                image.save(path, quality=quality)
                os.utime(path, (shared_mtime, shared_mtime))

            logs: list[str] = []
            phase_progress: list[tuple[str, int, int]] = []
            sorter = PhotoSorter(
                workers=1,
                avoid_duplicates=True,
                on_log=lambda msg, _lvl: logs.append(msg),
                on_phase_progress=lambda phase, current, total: phase_progress.append(
                    (phase, current, total)
                ),
            )
            success, processed, total, errors, skipped = sorter.sort_files(
                str(src), str(dest)
            )

            self.assertTrue(success)
            self.assertEqual((processed, total, errors, skipped), (2, 2, 0, 0))
            self.assertEqual(len(list(dest.rglob("*.jpg"))), 2)
            report = next(msg for msg in logs if "Похожее фото, сохранено" in msg)
            self.assertIn(str(src), report)
            self.assertIn("first.jpg", report)
            self.assertIn("second.jpg", report)
            self.assertTrue(
                any(
                    phase == "Поиск точных и похожих фотографий"
                    and current > 0
                    and total > 0
                    for phase, current, total in phase_progress
                )
            )
            self.assertIn(("Сортировка файлов", 0, 2), phase_progress)

    def test_avoid_duplicates_renames_unknown_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            dest = root / "dest"
            src.mkdir()
            dest.mkdir()
            shared_mtime = 1_700_000_000
            for i in range(3):
                path = src / f"f{i}.jpg"
                path.write_bytes(b"\xff\xd8\xff\xd9" + bytes([i]))
                os.utime(path, (shared_mtime, shared_mtime))

            sorter = PhotoSorter(workers=2, avoid_duplicates=True)
            success, processed, total, errors, skipped = sorter.sort_files(
                str(src), str(dest)
            )
            self.assertTrue(success)
            self.assertEqual(processed, 3)
            self.assertEqual(total, 3)
            self.assertEqual(errors, 0)
            self.assertEqual(skipped, 0)

            copied = list(dest.rglob("*.jpg"))
            self.assertEqual(len(copied), 3)
            self.assertTrue(all(p.parent.name == UNSORTED_DIR_NAME for p in copied))
            names = sorted(p.name for p in copied)
            self.assertEqual(len(set(names)), 3)


if __name__ == "__main__":
    unittest.main()
