"""Photo/video sorting engine — UI-agnostic."""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from statistics import median

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
PHASH_DISTANCE_THRESHOLD = 6
PHASH_SIZE = 32
PHASH_LOW_FREQUENCIES = 8

EXIF_DATE_TAGS = (
    Base.DateTimeOriginal,
    Base.DateTimeDigitized,
    Base.DateTime,
)

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[int, int, int, int], None]
PhaseProgressFn = Callable[[str, int, int], None]
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


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_hash(file_path: Path) -> int:
    """Return a 64-bit DCT perceptual hash for an image."""
    with Image.open(file_path) as image:
        pixels = list(
            image.convert("L")
            .resize((PHASH_SIZE, PHASH_SIZE), Image.Resampling.LANCZOS)
            .get_flattened_data()
        )

    size = PHASH_SIZE
    low = PHASH_LOW_FREQUENCIES
    scale = [math.sqrt(1 / size)] + [math.sqrt(2 / size)] * (low - 1)
    cosine = [
        [
            math.cos(math.pi * (2 * position + 1) * frequency / (2 * size))
            for position in range(size)
        ]
        for frequency in range(low)
    ]

    horizontal = [
        [
            scale[frequency]
            * sum(
                pixels[row * size + column] * cosine[frequency][column]
                for column in range(size)
            )
            for frequency in range(low)
        ]
        for row in range(size)
    ]
    coefficients = [
        [
            scale[vertical]
            * sum(
                horizontal[row][horizontal_frequency] * cosine[vertical][row]
                for row in range(size)
            )
            for horizontal_frequency in range(low)
        ]
        for vertical in range(low)
    ]

    values = [
        coefficients[row][column]
        for row in range(low)
        for column in range(low)
        if row or column
    ]
    threshold = median(values)
    result = 0
    for row in range(low):
        for column in range(low):
            result = (result << 1) | int(coefficients[row][column] > threshold)
    return result


def _phash_bands(value: int) -> tuple[int, ...]:
    return tuple((value >> (8 * band)) & 0xFF for band in range(8))


def _hamming_distance(first: int, second: int) -> int:
    return (first ^ second).bit_count()


def find_duplicate_candidates(
    files: list[Path],
    dest_path: Path,
    cancel_check: CancelFn,
    on_log: LogFn,
    on_progress: PhaseProgressFn,
) -> tuple[dict[Path, Path], dict[Path, Path], list[str]]:
    """Find exact and visually similar files against destination and source."""
    exact_matches: dict[Path, Path] = {}
    similar_matches: dict[Path, Path] = {}
    warnings: list[str] = []
    exact_index: dict[str, Path] = {}
    image_hashes: list[tuple[Path, int]] = []
    image_hash_ids: dict[int, int] = {}
    band_index: dict[int, set[int]] = {}

    def index_file(file_path: Path, *, source: bool) -> None:
        if cancel_check():
            return
        try:
            digest = sha256_file(file_path)
        except OSError as exc:
            warnings.append(
                f"Не удалось проверить дублирование {file_path.name}: {exc}"
            )
            return

        if digest in exact_index:
            if source:
                exact_matches[file_path] = exact_index[digest]
            return
        exact_index[digest] = file_path

        if file_path.suffix.lower() not in IMAGE_EXTENSIONS:
            return
        try:
            image_hash = perceptual_hash(file_path)
        except Exception as exc:
            warnings.append(
                f"Не удалось сравнить изображение {file_path.name}: {exc}"
            )
            return

        candidate_ids: set[int] = set()
        for band in _phash_bands(image_hash):
            candidate_ids.update(band_index.get(band, set()))

        closest: tuple[int, Path] | None = None
        for candidate_id in candidate_ids:
            candidate_path, candidate_hash = image_hashes[candidate_id]
            distance = _hamming_distance(image_hash, candidate_hash)
            if distance <= PHASH_DISTANCE_THRESHOLD and (
                closest is None or distance < closest[0]
            ):
                closest = distance, candidate_path

        if source and closest is not None:
            similar_matches[file_path] = closest[1]

        if image_hash not in image_hash_ids:
            image_id = len(image_hashes)
            image_hash_ids[image_hash] = image_id
            image_hashes.append((file_path, image_hash))
            for band in _phash_bands(image_hash):
                band_index.setdefault(band, set()).add(image_id)

    started_at = time.monotonic()
    existing_scanned = 0
    source_scanned = 0
    existing_files: list[Path] = []

    try:
        on_progress("Сканирование папки назначения для поиска дублей", 0, 0)
        on_log(f"Индексирую файлы в папке назначения: {dest_path}", "info")
        for existing in dest_path.rglob("*"):
            if cancel_check():
                break
            if existing.is_file() and existing.suffix.lower() in SUPPORTED_EXTENSIONS:
                existing_files.append(existing)

        work_total = len(existing_files) + len(files)
        progress_interval = max(1, work_total // 100)
        on_progress("Поиск точных и похожих фотографий", 0, work_total)
        for existing in existing_files:
            if cancel_check():
                break
            index_file(existing, source=False)
            existing_scanned += 1
            if existing_scanned % progress_interval == 0:
                on_progress(
                    "Поиск точных и похожих фотографий",
                    existing_scanned,
                    work_total,
                )
            if existing_scanned % 100 == 0:
                on_log(
                    f"Проверено файлов назначения: {existing_scanned}/"
                    f"{len(existing_files)}",
                    "info",
                )
        on_log(
            f"Индексирование папки назначения завершено: "
            f"{existing_scanned} файлов за {time.monotonic() - started_at:.1f} с",
            "info",
        )
        if not cancel_check():
            on_log(
                f"Сравниваю исходные файлы по SHA-256 и pHash: {len(files)} шт.",
                "info",
            )
            for file_path in files:
                if cancel_check():
                    break
                index_file(file_path, source=True)
                source_scanned += 1
                processed_for_index = existing_scanned + source_scanned
                if (
                    processed_for_index % progress_interval == 0
                    or processed_for_index == work_total
                ):
                    on_progress(
                        "Поиск точных и похожих фотографий",
                        processed_for_index,
                        work_total,
                    )
                if source_scanned % 100 == 0:
                    on_log(
                        f"Проверено исходных файлов: {source_scanned}/{len(files)}",
                        "info",
                    )
    except OSError as exc:
        warnings.append(f"Не удалось просканировать папку сохранения: {exc}")

    on_log(
        f"Проверка дублей завершена: исходных проверено {source_scanned}/{len(files)}, "
        f"точных копий {len(exact_matches)}, похожих фото {len(similar_matches)}, "
        f"время {time.monotonic() - started_at:.1f} с",
        "info",
    )
    return exact_matches, similar_matches, warnings


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
) -> Path:
    """Reserve a unique destination name safely across processes."""
    return reserve_unique_path(folder / filename)


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
    exact_duplicate_of: str | None = None,
    similar_to: str | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Worker entrypoint for ProcessPoolExecutor (must stay top-level / picklable).

    Returns (status, logs) where status is 'ok' | 'skipped' | 'error'.
    """
    logs: list[tuple[str, str]] = []
    path = Path(file_path)
    dest_path = Path(dest_dir)
    if exact_duplicate_of is not None:
        logs.append(
            (
                f"Точный дубль, не перенесён: исходный файл «{path}» "
                f"совпадает по SHA-256 с оригиналом «{exact_duplicate_of}»",
                "warn",
            )
        )
        return "skipped", logs

    year_hint: str | None = None
    device_hint: str | None = None
    reserved: Path | None = None
    try:
        metadata, meta_logs = get_file_metadata(path)
        logs.extend(meta_logs)
        if similar_to is not None:
            logs.append(
                (
                    f"Похожее фото, сохранено: исходный файл «{path}» "
                    f"похож по pHash на оригинал «{similar_to}»",
                    "warn",
                )
            )
        year_hint = metadata["year"]
        device_hint = metadata["device"]
        folder_name = device_folder_name(device_hint)
        device_folder = dest_path / year_hint / folder_name
        new_filename = safe_name(metadata["filename"], fallback="file")
        reserved = claim_destination_path(device_folder, new_filename)
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
        on_phase_progress: PhaseProgressFn | None = None,
    ):
        self.on_log = on_log or (lambda _msg, _level: None)
        self.on_progress = on_progress or (lambda _p, _t, _e, _s: None)
        self.cancel_check = cancel_check or (lambda: False)
        self.workers = workers
        self.avoid_duplicates = avoid_duplicates
        self.on_phase_progress = on_phase_progress or (
            lambda _phase, _current, _total: None
        )
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
    ) -> Path:
        return claim_destination_path(folder, filename)

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
        visited = 0
        self.log(f"Сканирую исходную папку: {source_path}", "info")
        self.on_phase_progress("Сканирование исходной папки", 0, 0)
        for path in source_path.rglob("*"):
            visited += 1
            if visited % 1000 == 0:
                self.on_phase_progress("Сканирование исходной папки", visited, 0)
                self.log(
                    f"Просмотрено элементов исходной папки: {visited}; "
                    f"поддерживаемых файлов найдено: {len(found)}",
                    "info",
                )
            if not path.is_file():
                continue
            if path.suffix.lower() not in self.supported_extensions:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
        self.log(
            f"Сканирование завершено: просмотрено элементов {visited}, "
            f"поддерживаемых файлов найдено {len(found)}",
            "info",
        )
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
            self.log(f"Папка назначения доступна: {dest_path}", "info")
            dest_resolved = dest_path.resolve()

            collected = self.collect_files(source_path)
            self.log(
                f"Обход источника завершён: найдено поддерживаемых файлов "
                f"{len(collected)}",
                "info",
            )
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
            self.log(
                f"К обработке допущено файлов: {total}; исключено из папки "
                f"назначения: {len(collected) - total}",
                "info",
            )

            if total == 0:
                self.log(
                    "Не найдено поддерживаемых файлов в указанной папке",
                    "warn",
                )
                return False, 0, 0, 0, 0

            exact_duplicates: dict[Path, Path] = {}
            similar_photos: dict[Path, Path] = {}
            if self.avoid_duplicates:
                self.log("Проверка точных и похожих фотографий…", "info")
                (
                    exact_duplicates,
                    similar_photos,
                    fingerprint_warnings,
                ) = find_duplicate_candidates(
                    files_to_process,
                    dest_path,
                    self.cancelled,
                    self.log,
                    self.on_phase_progress,
                )
                for warning in fingerprint_warnings:
                    self.log(warning, "warn")
                if self.cancelled():
                    self.log("Проверка дубликатов отменена", "warn")
                    return False, 0, total, 0, 0

            self.on_phase_progress("Сортировка файлов", 0, total)
            dup_note = (
                " · поиск точных и похожих фото"
                if self.avoid_duplicates
                else ""
            )
            self.log(
                f"Найдено {total} файлов для обработки · процессов: {workers}{dup_note}",
                "info",
            )
            self.log(
                f"Режим обработки: "
                f"{'последовательно' if workers == 1 else 'пул из ' + str(workers) + ' процессов'}",
                "info",
            )
            self.set_progress(0, total, 0, 0)
            was_cancelled = False
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
                        *sort_one_file(
                            str(file_path),
                            dest_str,
                            (
                                str(exact_duplicates[file_path])
                                if file_path in exact_duplicates
                                else None
                            ),
                            (
                                str(similar_photos[file_path])
                                if file_path in similar_photos
                                else None
                            ),
                        )
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
                                    (
                                        str(exact_duplicates[next_file])
                                        if next_file in exact_duplicates
                                        else None
                                    ),
                                    (
                                        str(similar_photos[next_file])
                                        if next_file in similar_photos
                                        else None
                                    ),
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
                            try:
                                handle_result(*future.result())
                            except Exception as exc:
                                self.log(
                                    f"Задание worker завершилось необработанной "
                                    f"ошибкой: {exc}",
                                    "error",
                                )
                                raise
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
