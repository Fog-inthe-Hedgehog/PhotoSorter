import json
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from PIL import Image
from PIL.ExifTags import IFD, TAGS, Base
from pillow_heif import register_heif_opener
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except ImportError:
    HAS_DND = False

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

EXIF_DATE_TAGS = (
    Base.DateTimeOriginal,
    Base.DateTimeDigitized,
    Base.DateTime,
)


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


def device_folder_name(device: str | None) -> str:
    """Folder under year: device name, or «не отсортировано» if unknown."""
    if not device or not str(device).strip():
        return UNSORTED_DIR_NAME
    return safe_name(str(device), fallback=UNSORTED_DIR_NAME)


def safe_name(name: str, fallback: str = "Unknown") -> str:
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid_chars else ch for ch in name)
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


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


def paths_overlap(source: Path, dest: Path) -> bool:
    try:
        source_resolved = source.resolve()
        dest_resolved = dest.resolve()
    except OSError:
        return False
    return (
        source_resolved == dest_resolved
        or source_resolved in dest_resolved.parents
        or dest_resolved in source_resolved.parents
    )


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


class PhotoSorterApp:
    def __init__(self, root, dnd_enabled: bool = False):
        self.root = root
        self.dnd_enabled = dnd_enabled
        self.root.title("Photo Sorter by Metadata")
        self.root.geometry("800x650")

        self.source_folder = tk.StringVar()
        self.destination_folder = tk.StringVar()
        self.status_text = tk.StringVar(value="Готов к работе")
        self.progress_var = tk.DoubleVar()

        self.supported_extensions = SUPPORTED_EXTENSIONS
        self.is_sorting = False
        self._sort_thread: threading.Thread | None = None

        self.setup_ui()
        if self.dnd_enabled:
            self.setup_drag_drop()

        if not ffprobe_available():
            self.log_message(
                "Предупреждение: ffprobe не найден — метаданные видео будут "
                "браться из времени изменения файла"
            )

    def ui_call(self, func, *args, **kwargs):
        """Schedule a callable on the Tk main thread."""
        self.root.after(0, lambda: func(*args, **kwargs))

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            main_frame,
            text="Сортировка фотографий по метаданным",
            font=("Arial", 16, "bold"),
        ).pack(pady=10)

        # Destination
        dest_frame = ttk.LabelFrame(main_frame, text="Папка для сохранения", padding="10")
        dest_frame.pack(fill=tk.X, pady=5)

        dest_row = ttk.Frame(dest_frame)
        dest_row.pack(fill=tk.X)
        ttk.Entry(dest_row, textvariable=self.destination_folder).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5)
        )
        ttk.Button(
            dest_row, text="Выбрать папку", command=self.select_destination_folder
        ).pack(side=tk.RIGHT)

        # Source
        source_frame = ttk.LabelFrame(
            main_frame, text="Папка с фотографиями (исходная)", padding="10"
        )
        source_frame.pack(fill=tk.X, pady=5)

        source_row = ttk.Frame(source_frame)
        source_row.pack(fill=tk.X)
        ttk.Entry(source_row, textvariable=self.source_folder).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5)
        )
        ttk.Button(
            source_row, text="Выбрать папку", command=self.select_source_folder
        ).pack(side=tk.RIGHT)

        # Drop / hint area
        drop_title = (
            "Перетащите папку с фотографиями сюда"
            if self.dnd_enabled
            else "Исходная папка"
        )
        drop_frame = ttk.LabelFrame(main_frame, text=drop_title, padding="10")
        drop_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        self.drop_area = tk.Text(
            drop_frame, height=8, bg="lightgray", font=("Arial", 12), wrap=tk.WORD
        )
        self.drop_area.pack(fill=tk.BOTH, expand=True)
        self._reset_drop_area_text()
        self.drop_area.config(state="disabled")

        # Progress
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=10)

        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100
        )
        self.progress_bar.pack(fill=tk.X, pady=5)
        ttk.Label(progress_frame, textvariable=self.status_text).pack()

        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(pady=10)

        self.start_button = ttk.Button(
            button_frame, text="Начать сортировку", command=self.start_sorting
        )
        self.start_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Очистить лог", command=self.clear_log).pack(
            side=tk.LEFT, padx=5
        )

        # Log with scrollbar in a frame
        log_frame = ttk.LabelFrame(main_frame, text="Лог операций", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        log_container = ttk.Frame(log_frame)
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_container, height=8, bg="white", font=("Consolas", 9), wrap=tk.WORD
        )
        scrollbar = ttk.Scrollbar(log_container, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _reset_drop_area_text(self):
        self.drop_area.config(state="normal")
        self.drop_area.delete("1.0", tk.END)
        if self.dnd_enabled:
            self.drop_area.insert(tk.END, "Перетащите папку с фотографиями сюда\n\n")
        else:
            self.drop_area.insert(
                tk.END,
                "Drag-and-drop недоступен. Выберите исходную папку кнопкой выше.\n\n",
            )
        self.drop_area.insert(tk.END, "Поддерживаемые форматы:\n")
        self.drop_area.insert(
            tk.END, "Фото: JPG, PNG, HEIC, WebP, GIF, BMP, TIFF\n"
        )
        self.drop_area.insert(
            tk.END, "Видео: MP4, MOV, AVI, 3GP, MKV, M4V, WMV, FLV, WEBM\n\n"
        )
        self.drop_area.insert(
            tk.END,
            "Структура: год съёмки / устройство / имя по дате изменения файла\n"
            f"Без устройства или при сбое: год / {UNSORTED_DIR_NAME}",
        )
        self.drop_area.config(state="disabled")

    def setup_drag_drop(self):
        self.drop_area.drop_target_register(DND_FILES)
        self.drop_area.dnd_bind("<<Drop>>", self.on_drop)
        self.drop_area.dnd_bind("<<DragEnter>>", self.on_drag_enter)
        self.drop_area.dnd_bind("<<DragLeave>>", self.on_drag_leave)

    def on_drag_enter(self, event):
        self.drop_area.config(bg="lightgreen")

    def on_drag_leave(self, event):
        self.drop_area.config(bg="lightgray")

    def on_drop(self, event):
        self.drop_area.config(bg="lightgray")
        files = self.root.tk.splitlist(event.data)
        if not files:
            return
        path = Path(files[0])
        if path.is_dir():
            self.set_source_folder(path)
        else:
            self.log_message("Перетащите папку, а не отдельные файлы")
            messagebox.showwarning("Предупреждение", "Перетащите папку с фотографиями")

    def set_source_folder(self, path: Path):
        self.source_folder.set(str(path))
        self.log_message(f"Выбрана исходная папка: {path}")
        self.drop_area.config(state="normal")
        self.drop_area.delete("1.0", tk.END)
        self.drop_area.insert(tk.END, f"Выбрана папка:\n{path}\n\n")
        self.drop_area.insert(tk.END, "Нажмите «Начать сортировку» для обработки")
        self.drop_area.config(state="disabled")

    def select_source_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку с фотографиями")
        if folder:
            self.set_source_folder(Path(folder))

    def select_destination_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку для сохранения")
        if folder:
            self.destination_folder.set(folder)
            self.log_message(f"Папка сохранения: {folder}")

    def log_message(self, message: str):
        def _log():
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
            self.log_text.see(tk.END)

        if threading.current_thread() is threading.main_thread():
            _log()
        else:
            self.ui_call(_log)

    def set_progress(self, processed: int, total: int):
        def _update():
            self.progress_var.set((processed / total) * 100 if total else 0)
            self.status_text.set(f"Обработано: {processed}/{total}")

        if threading.current_thread() is threading.main_thread():
            _update()
        else:
            self.ui_call(_update)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def get_modification_datetime(self, file_path: Path) -> datetime:
        try:
            return datetime.fromtimestamp(file_path.stat().st_mtime)
        except OSError:
            return datetime.now()

    def build_result(
        self,
        file_path: Path,
        capture_dt: datetime | None,
        device: str | None,
    ) -> dict:
        """Year from capture (fallback mtime); device may be None → unsorted; name from mtime."""
        mod_dt = self.get_modification_datetime(file_path)
        year_dt = capture_dt or mod_dt
        ext = file_path.suffix.lower()
        return {
            "datetime": capture_dt or mod_dt,
            "device": device,
            "year": year_dt.strftime("%Y"),
            "filename": f"{mod_dt.strftime('%Y-%m-%d_%H-%M-%S')}{ext}",
        }

    def extract_image_capture_info(
        self, file_path: Path
    ) -> tuple[datetime | None, str | None]:
        try:
            with Image.open(file_path) as img:
                exif = img.getexif()
                if not exif:
                    return None, None

                make = exif.get(Base.Make)
                model = exif.get(Base.Model)
                device = device_from_make_model(
                    decode_meta_str(make),
                    decode_meta_str(model),
                )

                capture_dt = None
                # Prefer DateTimeOriginal / Digitized from Exif IFD
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

                # Fallback: scan by tag name (some HEIC paths)
                if capture_dt is None:
                    for tag_id, value in exif.items():
                        tag_name = TAGS.get(tag_id, "")
                        if tag_name in (
                            "DateTimeOriginal",
                            "DateTimeDigitized",
                            "DateTime",
                        ):
                            capture_dt = parse_exif_datetime(value)
                            if capture_dt:
                                break

                return capture_dt, device
        except Exception as exc:
            self.log_message(f"Ошибка чтения EXIF {file_path.name}: {exc}")
            return None, None

    def extract_video_capture_info(
        self, file_path: Path
    ) -> tuple[datetime | None, str | None]:
        if not ffprobe_available():
            return None, None

        cmd = [
            "ffprobe",
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
            self.log_message(f"Ошибка ffprobe {file_path.name}: {exc}")
            return None, None

        if result.returncode != 0:
            return None, None

        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None, None

        tags: dict = {}
        if isinstance(data.get("format"), dict):
            tags.update(data["format"].get("tags") or {})
        for stream in data.get("streams") or []:
            if isinstance(stream, dict):
                tags.update(stream.get("tags") or {})

        # Normalize keys to lower for lookup
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

        return capture_dt, device

    def get_metadata(self, file_path: Path) -> dict:
        file_path = Path(file_path)
        ext = file_path.suffix.lower()

        if ext in VIDEO_EXTENSIONS:
            capture_dt, device = self.extract_video_capture_info(file_path)
        else:
            capture_dt, device = self.extract_image_capture_info(file_path)

        return self.build_result(file_path, capture_dt, device)

    def copy_to_unsorted(
        self,
        file_path: Path,
        dest_path: Path,
        year: str | None = None,
    ) -> Path | None:
        """Copy file into {year}/Unknown Devices/. Returns destination path or None."""
        try:
            if not year:
                year = self.get_modification_datetime(file_path).strftime("%Y")
            folder = dest_path / year / UNSORTED_DIR_NAME
            folder.mkdir(parents=True, exist_ok=True)

            mod_dt = self.get_modification_datetime(file_path)
            filename = safe_name(
                f"{mod_dt.strftime('%Y-%m-%d_%H-%M-%S')}{file_path.suffix.lower()}",
                fallback="file",
            )
            new_file_path = unique_path(folder / filename)
            shutil.copy2(file_path, new_file_path)
            return new_file_path
        except Exception as exc:
            self.log_message(
                f"Не удалось поместить в «{UNSORTED_DIR_NAME}» {file_path.name}: {exc}"
            )
            return None

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

    def sort_files(self, source_dir: str, dest_dir: str) -> tuple[bool, int, int, int]:
        """Returns (success, processed, total, error_count). success iff error_count == 0."""
        source_path = Path(source_dir)
        dest_path = Path(dest_dir)
        processed = 0
        error_count = 0
        total = 0

        try:
            if not source_path.exists():
                self.log_message(f"Ошибка: папка {source_dir} не существует")
                return False, 0, 0, 1

            dest_path.mkdir(parents=True, exist_ok=True)
            dest_resolved = dest_path.resolve()

            files_to_process = self.collect_files(source_path)
            total = len(files_to_process)

            if total == 0:
                self.log_message("Не найдено поддерживаемых файлов в указанной папке")
                return False, 0, 0, 0

            self.log_message(f"Найдено {total} файлов для обработки")

            for file_path in files_to_process:
                year_hint: str | None = None
                try:
                    # Skip files already under destination to avoid re-copy loops
                    try:
                        file_path.resolve().relative_to(dest_resolved)
                        continue
                    except ValueError:
                        pass

                    metadata = self.get_metadata(file_path)
                    year_hint = metadata["year"]
                    folder_name = device_folder_name(metadata["device"])
                    device_folder = dest_path / metadata["year"] / folder_name
                    device_folder.mkdir(parents=True, exist_ok=True)

                    new_filename = safe_name(metadata["filename"], fallback="file")
                    new_file_path = unique_path(device_folder / new_filename)

                    shutil.copy2(file_path, new_file_path)
                    processed += 1
                    self.set_progress(processed, total)
                    if folder_name == UNSORTED_DIR_NAME:
                        self.log_message(
                            f"Без устройства → {new_file_path}"
                        )
                    else:
                        self.log_message(f"OK {file_path.name} -> {new_file_path}")
                except Exception as exc:
                    self.log_message(f"Ошибка сортировки {file_path}: {exc}")
                    fallback = self.copy_to_unsorted(
                        file_path, dest_path, year=year_hint
                    )
                    if fallback is not None:
                        processed += 1
                        self.set_progress(processed, total)
                        self.log_message(
                            f"Восстановлено → {fallback}"
                        )
                    else:
                        error_count += 1

            success = error_count == 0
            self.log_message(
                f"Сортировка завершена! Обработано {processed} из {total} файлов"
                + (f", ошибок: {error_count}" if error_count else "")
            )
            return success, processed, total, error_count
        except Exception as exc:
            error_count += 1
            self.log_message(f"Критическая ошибка сортировки: {exc}")
            return False, processed, total, error_count

    def _sorting_finished(
        self, success: bool, processed: int, total: int, error_count: int
    ):
        self.is_sorting = False
        self.start_button.config(state="normal")
        if total == 0 and error_count == 0:
            self.status_text.set("Файлы не найдены")
            messagebox.showwarning("Предупреждение", "Не найдено поддерживаемых файлов")
        elif success:
            self.progress_var.set(100)
            self.status_text.set(f"Готово: {processed}/{total}")
            messagebox.showinfo(
                "Готово",
                f"Сортировка завершена.\nОбработано файлов: {processed} из {total}",
            )
        else:
            self.status_text.set(
                f"Ошибки: {error_count}; обработано {processed}/{total}"
            )
            messagebox.showwarning(
                "Внимание",
                f"Сортировка завершена с ошибками.\n"
                f"Обработано: {processed} из {total}\n"
                f"Ошибок: {error_count}",
            )

    def start_sorting(self):
        if self.is_sorting:
            messagebox.showinfo("Информация", "Сортировка уже выполняется")
            return

        source = self.source_folder.get().strip()
        destination = self.destination_folder.get().strip()

        if not source:
            messagebox.showwarning("Предупреждение", "Выберите папку с фотографиями")
            return
        if not destination:
            messagebox.showwarning("Предупреждение", "Выберите папку для сохранения")
            return

        source_path = Path(source)
        dest_path = Path(destination)

        if not source_path.is_dir():
            messagebox.showwarning("Предупреждение", "Исходная папка не существует")
            return

        if paths_overlap(source_path, dest_path):
            messagebox.showerror(
                "Ошибка",
                "Папка сохранения не должна совпадать с исходной "
                "и не должна быть внутри неё (и наоборот).",
            )
            return

        self.is_sorting = True
        self.start_button.config(state="disabled")
        self.status_text.set("Сортировка начата...")
        self.progress_var.set(0)

        def worker():
            success, processed, total, error_count = self.sort_files(
                source, destination
            )
            self.ui_call(
                self._sorting_finished, success, processed, total, error_count
            )

        self._sort_thread = threading.Thread(target=worker, daemon=True)
        self._sort_thread.start()


def main():
    dnd_enabled = False
    if HAS_DND:
        try:
            root = TkinterDnD.Tk()
            dnd_enabled = True
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()

    if not dnd_enabled:
        messagebox.showinfo(
            "Информация",
            "Drag-and-drop недоступен (нужен пакет tkinterdnd2).\n"
            "Используйте кнопку «Выбрать папку» для исходной директории.",
        )

    PhotoSorterApp(root, dnd_enabled=dnd_enabled)
    root.mainloop()


if __name__ == "__main__":
    main()
