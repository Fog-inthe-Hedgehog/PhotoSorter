"""Photo/video sorting engine — UI-agnostic."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ExifTags import IFD, TAGS, Base
from pillow_heif import register_heif_opener

register_heif_opener()

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".3gp",
    ".m4v",
    ".wmv",
    ".flv",
    ".webm",
    ".mpeg",
    ".mpg",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".heic",
    ".heif",
    ".webp",
    ".ico",
}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
UNSORTED_DIR_NAME = "Unknown Device"
JPEG_EXTENSIONS = {".jpg", ".jpeg"}

EXIF_DATE_TAGS = (
    Base.DateTimeOriginal,
    Base.DateTimeDigitized,
    Base.DateTime,
)

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[int, int, int, int], None]
CancelFn = Callable[[], bool]

_ffprobe_cached: bool | None = None
_ffprobe_path: str | None = None


def decode_meta_str(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    elif not isinstance(value, str):
        value = str(value)
    value = value.strip().strip("\x00")
    return value or None


def parse_exif_datetime(value: str | bytes | None) -> datetime | None:
    text = decode_meta_str(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def parse_video_datetime(value: str | bytes | None) -> datetime | None:
    text = decode_meta_str(value)
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def device_from_make_model(make: str | None, model: str | None) -> str | None:
    make = (make or "").strip()
    model = (model or "").strip()
    if make and model:
        if model.lower().startswith(make.lower()):
            return model
        return f"{make} {model}".strip()
    return make or model or None


def safe_name(name: str, fallback: str = "Unknown") -> str:
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid_chars else ch for ch in name)
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def device_folder_name(device: str | None) -> str:
    if not device or not str(device).strip():
        return UNSORTED_DIR_NAME
    return safe_name(str(device), fallback=UNSORTED_DIR_NAME)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def reserve_unique_path(path: Path) -> Path:
    """Atomically claim a unique path by creating an empty placeholder file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stem, suffix = path.stem, path.suffix
    counter = 0
    while True:
        candidate = (
            path if counter == 0 else path.with_name(f"{stem}_{counter}{suffix}")
        )
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            counter += 1


def default_worker_count() -> int:
    return min(8, max(4, os.cpu_count() or 4))


def _pool_worker_init() -> None:
    """Re-init native openers / caches inside each spawned process."""
    global _ffprobe_cached, _ffprobe_path
    _ffprobe_cached = None
    _ffprobe_path = None
    register_heif_opener()


def claim_destination_path(
    folder: Path,
    filename: str,
    *,
    known_device: bool,
    avoid_duplicates: bool,
) -> Path | None:
    """Reserve destination path. Returns None when skipping a device duplicate.

    Uses O_EXCL so it is safe across threads and processes without a lock.
    """
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    if avoid_duplicates and known_device:
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return target
        except FileExistsError:
            return None
    return reserve_unique_path(target)


def extract_image_capture_info(
    file_path: Path,
) -> tuple[datetime | None, str | None, str | None]:
    """Returns (capture_dt, device, warning_or_none)."""
    try:
        ext = file_path.suffix.lower()
        if ext in JPEG_EXTENSIONS:
            raw = read_jpeg_exif_bytes(file_path)
            if raw:
                exif = Image.Exif()
                exif.load(raw)
                capture_dt, device = capture_info_from_exif(exif)
                return capture_dt, device, None

        with Image.open(file_path) as img:
            capture_dt, device = capture_info_from_exif(img.getexif())
            return capture_dt, device, None
    except Exception as exc:
        return None, None, f"Ошибка чтения EXIF {file_path.name}: {exc}"


def extract_video_capture_info(
    file_path: Path,
) -> tuple[datetime | None, str | None, str | None]:
    """Returns (capture_dt, device, warning_or_none)."""
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        return None, None, None

    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, None, f"Ошибка ffprobe {file_path.name}: {exc}"

    if result.returncode != 0:
        return None, None, None

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None, None, None

    tags: dict = {}
    if isinstance(data.get("format"), dict):
        tags.update(data["format"].get("tags") or {})
    for stream in data.get("streams") or []:
        if isinstance(stream, dict):
            tags.update(stream.get("tags") or {})

    lower_tags = {str(k).lower(): v for k, v in tags.items()}

    make = lower_tags.get("make") or lower_tags.get("com.apple.quicktime.make")
    model = lower_tags.get("model") or lower_tags.get("com.apple.quicktime.model")
    device = device_from_make_model(
        decode_meta_str(make if make is not None else None),
        decode_meta_str(model if model is not None else None),
    )

    capture_dt = None
    date_keys = (
        "creation_time",
        "com.apple.quicktime.creationdate",
        "date",
        "date_time_original",
    )
    for key in date_keys:
        if key in lower_tags:
            capture_dt = parse_video_datetime(lower_tags[key])
            if capture_dt:
                break

    if capture_dt is None:
        for key, value in lower_tags.items():
            if "creation_time" in key or key.endswith("date"):
                capture_dt = parse_video_datetime(value)
                if capture_dt:
                    break

    return capture_dt, device, None


def get_file_metadata(
    file_path: Path,
) -> tuple[dict, list[tuple[str, str]]]:
    """Returns (metadata_dict, log_messages)."""
    file_path = Path(file_path)
    logs: list[tuple[str, str]] = []
    ext = file_path.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        capture_dt, device, warn = extract_video_capture_info(file_path)
    else:
        capture_dt, device, warn = extract_image_capture_info(file_path)
    if warn:
        logs.append((warn, "warn"))
    return build_result(file_path, capture_dt, device), logs


def copy_fallback_file(
    file_path: Path,
    dest_path: Path,
    year: str | None = None,
    device: str | None = None,
) -> tuple[Path | None, str | None]:
    """Copy into year/device. Returns (path_or_none, error_or_none)."""
    try:
        if not year:
            year = get_modification_datetime(file_path).strftime("%Y")
        folder = dest_path / year / device_folder_name(device)
        folder.mkdir(parents=True, exist_ok=True)

        mod_dt = get_modification_datetime(file_path)
        filename = safe_name(
            f"{mod_dt.strftime('%Y-%m-%d_%H-%M-%S')}{file_path.suffix.lower()}",
            fallback="file",
        )
        new_file_path = reserve_unique_path(folder / filename)
        shutil.copy2(file_path, new_file_path)
        return new_file_path, None
    except Exception as exc:
        return None, f"Не удалось сохранить fallback {file_path.name}: {exc}"


def sort_one_file(
    file_path: str,
    dest_dir: str,
    avoid_duplicates: bool,
) -> tuple[str, list[tuple[str, str]]]:
    """Worker entrypoint for ProcessPoolExecutor (must stay top-level / picklable).

    Returns (status, logs) where status is 'ok' | 'skipped' | 'error'.
    """
    logs: list[tuple[str, str]] = []
    path = Path(file_path)
    dest_path = Path(dest_dir)
    year_hint: str | None = None
    device_hint: str | None = None
    reserved: Path | None = None
    try:
        metadata, meta_logs = get_file_metadata(path)
        logs.extend(meta_logs)
        year_hint = metadata["year"]
        device_hint = metadata["device"]
        folder_name = device_folder_name(device_hint)
        known_device = folder_name != UNSORTED_DIR_NAME
        device_folder = dest_path / year_hint / folder_name
        new_filename = safe_name(metadata["filename"], fallback="file")
        reserved = claim_destination_path(
            device_folder,
            new_filename,
            known_device=known_device,
            avoid_duplicates=avoid_duplicates,
        )
        if reserved is None:
            logs.append(
                (
                    f"Пропуск (дубликат): {path.name} → "
                    f"{year_hint}/{folder_name}/{new_filename}",
                    "warn",
                )
            )
            return "skipped", logs

        shutil.copy2(path, reserved)
        return "ok", logs
    except Exception as exc:
        if reserved is not None:
            try:
                reserved.unlink(missing_ok=True)
            except OSError:
                pass
        logs.append((f"Ошибка сортировки {path}: {exc}", "warn"))
        fallback, err = copy_fallback_file(
            path, dest_path, year=year_hint, device=device_hint
        )
        if fallback is not None:
            logs.append((f"Восстановлено → {fallback}", "warn"))
            return "ok", logs
        if err:
            logs.append((err, "error"))
        return "error", logs


def paths_overlap(source: Path, dest: Path) -> bool:
    try:
        source_resolved = source.resolve()
        dest_resolved = dest.resolve()
    except OSError:
        # Fail closed: block sort if paths cannot be verified.
        return True
    return (
        source_resolved == dest_resolved
        or source_resolved in dest_resolved.parents
        or dest_resolved in source_resolved.parents
    )


def get_ffprobe_path() -> str | None:
    global _ffprobe_cached, _ffprobe_path
    if _ffprobe_cached is None:
        ffprobe_path = shutil.which("ffprobe")
        if not ffprobe_path:
            app_dir = Path(__file__).resolve().parent
            bundled = app_dir / ("ffprobe.exe" if sys.platform.startswith("win") else "ffprobe")
            if bundled.exists():
                ffprobe_path = str(bundled)
        if not ffprobe_path and getattr(sys, "_MEIPASS", None):
            bundled = Path(sys._MEIPASS) / ("ffprobe.exe" if sys.platform.startswith("win") else "ffprobe")
            if bundled.exists():
                ffprobe_path = str(bundled)
        _ffprobe_path = ffprobe_path
        _ffprobe_cached = ffprobe_path is not None
    return _ffprobe_path


def ffprobe_available() -> bool:
    return get_ffprobe_path() is not None


def get_modification_datetime(file_path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    except OSError:
        return datetime.now()


def build_result(
    file_path: Path,
    capture_dt: datetime | None,
    device: str | None,
) -> dict:
    mod_dt = get_modification_datetime(file_path)
    year_dt = capture_dt or mod_dt
    ext = file_path.suffix.lower()
    return {
        "datetime": capture_dt or mod_dt,
        "device": device,
        "year": year_dt.strftime("%Y"),
        "filename": f"{mod_dt.strftime('%Y-%m-%d_%H-%M-%S')}{ext}",
    }


def read_jpeg_exif_bytes(file_path: Path) -> bytes | None:
    """Read JPEG APP1 Exif payload without decoding pixel data."""
    try:
        with open(file_path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = f.read(2)
                if len(marker) < 2:
                    return None
                if marker[0] != 0xFF:
                    return None
                while marker[1] == 0xFF:
                    nxt = f.read(1)
                    if not nxt:
                        return None
                    marker = b"\xff" + nxt

                m = marker[1]
                if m in (0xD8, 0xD9):  # SOI / EOI
                    return None
                if m == 0xDA:  # SOS — image data starts
                    return None
                # Standalone markers have no length
                if 0xD0 <= m <= 0xD7 or m == 0x01:
                    continue

                length_bytes = f.read(2)
                if len(length_bytes) < 2:
                    return None
                length = int.from_bytes(length_bytes, "big")
                if length < 2:
                    return None
                payload = f.read(length - 2)
                if len(payload) < length - 2:
                    return None
                if m == 0xE1 and payload.startswith(b"Exif\x00\x00"):
                    return payload
    except OSError:
        return None
    return None


def capture_info_from_exif(
    exif: Image.Exif,
) -> tuple[datetime | None, str | None]:
    if not exif:
        return None, None

    make = exif.get(Base.Make)
    model = exif.get(Base.Model)
    device = device_from_make_model(
        decode_meta_str(make),
        decode_meta_str(model),
    )

    capture_dt = None
    try:
        exif_ifd = exif.get_ifd(IFD.Exif)
    except Exception:
        exif_ifd = {}

    for tag in (Base.DateTimeOriginal, Base.DateTimeDigitized):
        if exif_ifd and tag in exif_ifd:
            capture_dt = parse_exif_datetime(exif_ifd.get(tag))
            if capture_dt:
                break

    if capture_dt is None:
        for tag in EXIF_DATE_TAGS:
            value = exif.get(tag)
            if value:
                capture_dt = parse_exif_datetime(value)
                if capture_dt:
                    break

    if capture_dt is None:
        for tag_id, value in exif.items():
            tag_name = TAGS.get(tag_id, "")
            if tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                capture_dt = parse_exif_datetime(value)
                if capture_dt:
                    break

    return capture_dt, device


class PhotoSorter:
    def __init__(
        self,
        on_log: LogFn | None = None,
        on_progress: ProgressFn | None = None,
        cancel_check: CancelFn | None = None,
        workers: int | None = None,
        avoid_duplicates: bool = False,
    ):
        self.on_log = on_log or (lambda _msg, _level: None)
        self.on_progress = on_progress or (lambda _p, _t, _e, _s: None)
        self.cancel_check = cancel_check or (lambda: False)
        self.workers = workers
        self.avoid_duplicates = avoid_duplicates
        self.supported_extensions = SUPPORTED_EXTENSIONS
        self._has_ffprobe = ffprobe_available()

    def log(self, message: str, level: str = "info") -> None:
        self.on_log(message, level)

    def set_progress(
        self,
        processed: int,
        total: int,
        error_count: int = 0,
        skipped: int = 0,
    ) -> None:
        self.on_progress(processed, total, error_count, skipped)

    def cancelled(self) -> bool:
        try:
            return bool(self.cancel_check())
        except Exception:
            return False

    def extract_image_capture_info(
        self, file_path: Path
    ) -> tuple[datetime | None, str | None]:
        capture_dt, device, warn = extract_image_capture_info(file_path)
        if warn:
            self.log(warn, "warn")
        return capture_dt, device

    def extract_video_capture_info(
        self, file_path: Path
    ) -> tuple[datetime | None, str | None]:
        capture_dt, device, warn = extract_video_capture_info(file_path)
        if warn:
            self.log(warn, "warn")
        return capture_dt, device

    def get_metadata(self, file_path: Path) -> dict:
        metadata, logs = get_file_metadata(file_path)
        for msg, level in logs:
            self.log(msg, level)
        return metadata

    def claim_destination(
        self,
        folder: Path,
        filename: str,
        *,
        known_device: bool,
    ) -> Path | None:
        return claim_destination_path(
            folder,
            filename,
            known_device=known_device,
            avoid_duplicates=self.avoid_duplicates,
        )

    def copy_fallback(
        self,
        file_path: Path,
        dest_path: Path,
        year: str | None = None,
        device: str | None = None,
    ) -> Path | None:
        out, err = copy_fallback_file(file_path, dest_path, year=year, device=device)
        if err:
            self.log(err, "error")
        return out

    def copy_to_unsorted(
        self,
        file_path: Path,
        dest_path: Path,
        year: str | None = None,
    ) -> Path | None:
        return self.copy_fallback(file_path, dest_path, year=year, device=None)

    def collect_files(self, source_path: Path) -> list[Path]:
        found: list[Path] = []
        seen: set[Path] = set()
        for path in source_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self.supported_extensions:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
        return found

    def _apply_job_result(self, status: str, logs: list[tuple[str, str]]) -> tuple[bool, bool]:
        """Apply worker result. Returns (ok_counted, skipped_dup)."""
        for msg, level in logs:
            self.log(msg, level)
        if status == "skipped":
            return True, True
        if status == "ok":
            return True, False
        return False, False

    def sort_files(
        self,
        source_dir: str,
        dest_dir: str,
        workers: int | None = None,
        avoid_duplicates: bool | None = None,
    ) -> tuple[bool, int, int, int, int]:
        """Returns (success, processed, total, error_count, skipped)."""
        source_path = Path(source_dir)
        dest_path = Path(dest_dir)
        processed = 0
        error_count = 0
        skipped = 0
        total = 0

        if workers is None:
            workers = self.workers
        if workers is None:
            workers = default_worker_count()
        workers = max(1, int(workers))

        if avoid_duplicates is not None:
            self.avoid_duplicates = bool(avoid_duplicates)

        try:
            if not source_path.exists():
                self.log(f"Ошибка: папка {source_dir} не существует", "error")
                return False, 0, 0, 1, 0

            dest_path.mkdir(parents=True, exist_ok=True)
            dest_resolved = dest_path.resolve()

            collected = self.collect_files(source_path)
            files_to_process: list[Path] = []
            for file_path in collected:
                try:
                    file_path.resolve().relative_to(dest_resolved)
                    self.log(
                        f"Пропуск (внутри папки сохранения): {file_path}",
                        "warn",
                    )
                except ValueError:
                    files_to_process.append(file_path)
                except OSError as exc:
                    self.log(f"Пропуск (недоступен путь) {file_path}: {exc}", "warn")

            total = len(files_to_process)

            if total == 0:
                self.log(
                    "Не найдено поддерживаемых файлов в указанной папке",
                    "warn",
                )
                return False, 0, 0, 0, 0

            dup_note = (
                " · без дубликатов устройств"
                if self.avoid_duplicates
                else ""
            )
            self.log(
                f"Найдено {total} файлов для обработки · процессов: {workers}{dup_note}",
                "info",
            )
            self.set_progress(0, total, 0, 0)

            was_cancelled = False
            avoid = self.avoid_duplicates
            dest_str = str(dest_path)

            def bump(ok: bool, *, skipped_dup: bool = False) -> None:
                nonlocal processed, error_count, skipped
                if ok:
                    processed += 1
                    if skipped_dup:
                        skipped += 1
                else:
                    error_count += 1
                self.set_progress(processed, total, error_count, skipped)

            def handle_result(status: str, logs: list[tuple[str, str]]) -> None:
                ok, skipped_dup = self._apply_job_result(status, logs)
                bump(ok, skipped_dup=skipped_dup)

            if workers == 1:
                for file_path in files_to_process:
                    if self.cancelled():
                        was_cancelled = True
                        break
                    handle_result(
                        *sort_one_file(str(file_path), dest_str, avoid)
                    )
            else:
                # spawn: safe when the parent already has GUI/threads (Flet).
                ctx = multiprocessing.get_context("spawn")
                pending: set = set()
                file_iter = iter(files_to_process)
                executor = ProcessPoolExecutor(
                    max_workers=workers,
                    mp_context=ctx,
                    initializer=_pool_worker_init,
                )
                try:

                    def submit_available() -> None:
                        nonlocal was_cancelled
                        while len(pending) < workers:
                            if self.cancelled():
                                was_cancelled = True
                                return
                            try:
                                next_file = next(file_iter)
                            except StopIteration:
                                return
                            pending.add(
                                executor.submit(
                                    sort_one_file,
                                    str(next_file),
                                    dest_str,
                                    avoid,
                                )
                            )

                    submit_available()
                    while pending:
                        if self.cancelled():
                            was_cancelled = True
                            break
                        done, pending = wait(
                            pending, return_when=FIRST_COMPLETED
                        )
                        for future in done:
                            handle_result(*future.result())
                        if not was_cancelled:
                            submit_available()
                finally:
                    executor.shutdown(wait=True, cancel_futures=was_cancelled)

            if was_cancelled or self.cancelled():
                self.log(
                    f"Сортировка отменена · обработано {processed} из {total}"
                    + (f", дубликатов: {skipped}" if skipped else ""),
                    "warn",
                )
                return False, processed, total, error_count, skipped

            success = error_count == 0
            extra = ""
            if error_count:
                extra += f", ошибок: {error_count}"
            if skipped:
                extra += f", пропущено дубликатов: {skipped}"
            self.log(
                f"Сортировка завершена! Обработано {processed} из {total} файлов"
                + extra,
                "ok" if success else "error",
            )
            return success, processed, total, error_count, skipped
        except Exception as exc:
            error_count += 1
            self.log(f"Критическая ошибка сортировки: {exc}", "error")
            return False, processed, total, error_count, skipped
