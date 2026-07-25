"""Photo Sorter — Flet desktop UI."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import flet as ft
import flet_dropzone as ftd

from sorter import (
    UNSORTED_DIR_NAME,
    PhotoSorter,
    ffprobe_available,
    paths_overlap,
)

# Theme — light slate + teal accent
BG = "#F4F6F8"
SURFACE = "#FFFFFF"
BORDER = "#D7DEE7"
TEXT = "#1C2430"
MUTED = "#5B6B7C"
ACCENT = "#0F766E"
ACCENT_SOFT = "#CCFBF1"
DROP_IDLE = "#EEF2F6"
LOG_BG = "#1C2430"
OK = "#15803D"
WARN = "#B45309"
PILL_OK_BG = "#DCFCE7"
PILL_WARN_BG = "#FEF3C7"
PILL_MUTE_BG = "#E8EEF4"

LOG_COLORS = {
    "info": "#E8EEF4",
    "ok": "#4ADE80",
    "warn": "#FBBF24",
    "error": "#F87171",
}

# Coalesce worker UI updates onto the event loop (ms).
UI_BATCH_MS = 100


def shorten_path(path: str, max_len: int = 64) -> str:
    if len(path) <= max_len:
        return path
    return "…" + path[-(max_len - 1) :]


async def pick_directory(title: str) -> str | None:
    """Native directory picker (FilePicker → Zenity fallback)."""
    try:
        path = await ft.FilePicker().get_directory_path(dialog_title=title)
        if path:
            return path
    except Exception:
        pass

    zenity = shutil.which("zenity")
    if zenity:
        try:
            result = subprocess.run(
                [zenity, "--file-selection", "--directory", f"--title={title}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                chosen = (result.stdout or "").strip()
                return chosen or None
        except OSError:
            pass
    return None


def main(page: ft.Page) -> None:
    page.title = "Photo Sorter"
    page.window.width = 920
    page.window.height = 740
    page.window.min_width = 780
    page.window.min_height = 640
    page.bgcolor = BG
    page.padding = 20
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(
        color_scheme_seed=ACCENT,
        font_family="Roboto",
    )

    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()

    source_path = ""
    dest_path = ""
    is_sorting = False
    has_ffprobe = ffprobe_available()

    # Batched UI state (mutated only on the asyncio loop)
    pending_logs: list[tuple[str, str]] = []
    pending_progress: tuple[int, int, int] | None = None
    flush_task: asyncio.Task | None = None

    dest_field = ft.TextField(
        label="Папка сохранения",
        hint_text="Укажите путь или нажмите «Обзор…»",
        expand=True,
        border_color=BORDER,
        focused_border_color=ACCENT,
        bgcolor=SURFACE,
    )
    source_field = ft.TextField(
        label="Исходная папка",
        hint_text="Укажите путь или нажмите «Обзор…»",
        expand=True,
        border_color=BORDER,
        focused_border_color=ACCENT,
        bgcolor=SURFACE,
    )

    drop_title = ft.Text(
        "Перетащите папку сюда",
        size=16,
        weight=ft.FontWeight.BOLD,
        color=TEXT,
        text_align=ft.TextAlign.CENTER,
    )
    drop_detail = ft.Text(
        "JPG, PNG, HEIC, WebP · MP4, MOV, 3GP и др.\n"
        "Или нажмите, чтобы выбрать папку",
        size=12,
        color=MUTED,
        text_align=ft.TextAlign.CENTER,
    )
    drop_zone = ft.Container(
        content=ft.Column(
            [drop_title, drop_detail],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
            tight=True,
        ),
        bgcolor=DROP_IDLE,
        border=ft.Border.all(1, BORDER),
        border_radius=12,
        padding=28,
        alignment=ft.Alignment.CENTER,
        ink=True,
        height=120,
    )

    def on_drop_entered(_e=None) -> None:
        if is_sorting:
            return
        drop_zone.bgcolor = ACCENT_SOFT
        drop_zone.border = ft.Border.all(2, ACCENT)
        drop_title.value = "Отпустите, чтобы выбрать"
        page.update()

    def on_drop_exited(_e=None) -> None:
        if is_sorting:
            return
        if source_path:
            p = Path(source_path)
            drop_title.value = p.name or source_path
            drop_detail.value = shorten_path(source_path, 72)
            drop_zone.bgcolor = ACCENT_SOFT
            drop_zone.border = ft.Border.all(2, ACCENT)
        else:
            drop_title.value = "Перетащите папку сюда"
            drop_detail.value = (
                "JPG, PNG, HEIC, WebP · MP4, MOV, 3GP и др.\n"
                "Или нажмите, чтобы выбрать папку"
            )
            drop_zone.bgcolor = DROP_IDLE
            drop_zone.border = ft.Border.all(1, BORDER)
        page.update()

    def on_files_dropped(e: ftd.DropzoneEvent) -> None:
        if is_sorting:
            return
        files = list(e.files or [])
        if not files:
            on_drop_exited()
            return

        path = Path(files[0])
        if path.is_dir():
            set_source(str(path))
            return
        if path.is_file():
            show_dialog(
                "Предупреждение",
                "Перетащите папку с фотографиями, а не отдельный файл.",
            )
            on_drop_exited()
            return

        # Some desktops report a path that exists only after resolve
        resolved = path.resolve() if path.exists() else path
        if resolved.is_dir():
            set_source(str(resolved))
        else:
            show_dialog(
                "Предупреждение",
                "Перетащите папку с фотографиями, а не отдельный файл.",
            )
            on_drop_exited()

    drop_target = ftd.Dropzone(
        content=drop_zone,
        on_dropped=on_files_dropped,
        on_entered=on_drop_entered,
        on_exited=on_drop_exited,
        expand=False,
        height=120,
    )

    pill_source = ft.Container(
        content=ft.Text("Source · не выбран", size=12, color=MUTED),
        bgcolor=PILL_MUTE_BG,
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        border_radius=20,
    )
    pill_dest = ft.Container(
        content=ft.Text("Dest · не выбран", size=12, color=MUTED),
        bgcolor=PILL_MUTE_BG,
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        border_radius=20,
    )
    pill_video = ft.Container(
        content=ft.Text(
            "Video meta · ffprobe OK" if has_ffprobe else "Video meta · без ffprobe",
            size=12,
            color=OK if has_ffprobe else WARN,
        ),
        bgcolor=PILL_OK_BG if has_ffprobe else PILL_WARN_BG,
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        border_radius=20,
    )

    progress_bar = ft.ProgressBar(
        value=0,
        color=ACCENT,
        bgcolor=DROP_IDLE,
        bar_height=10,
        border_radius=6,
    )
    status_text = ft.Text("Готов к работе", size=12, color=MUTED)

    start_btn = ft.Button(
        "Начать сортировку",
        icon=ft.Icons.PLAY_ARROW_ROUNDED,
        bgcolor=ACCENT,
        color=ft.Colors.WHITE,
        disabled=True,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.Padding.symmetric(horizontal=20, vertical=14),
        ),
    )
    cancel_btn = ft.OutlinedButton(
        "Отмена",
        icon=ft.Icons.STOP_ROUNDED,
        disabled=True,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
        ),
    )
    clear_btn = ft.OutlinedButton(
        "Очистить лог",
        icon=ft.Icons.DELETE_OUTLINE,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
        ),
    )
    save_log_btn = ft.OutlinedButton(
        "Сохранить лог",
        icon=ft.Icons.SAVE_OUTLINED,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=10),
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
        ),
    )
    browse_dest_btn = ft.OutlinedButton("Обзор…", icon=ft.Icons.FOLDER_OPEN)
    browse_source_btn = ft.OutlinedButton("Обзор…", icon=ft.Icons.FOLDER_OPEN)

    log_list = ft.ListView(expand=True, spacing=2, auto_scroll=True, padding=12)
    log_panel = ft.Container(
        content=log_list,
        bgcolor=LOG_BG,
        border_radius=10,
        expand=True,
        padding=0,
    )

    def show_dialog(title: str, message: str) -> None:
        def close(_e=None):
            page.pop_dialog()

        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(title),
                content=ft.Text(message),
                actions=[ft.TextButton("OK", on_click=close)],
                modal=True,
            )
        )

    def _append_log_control(message: str, level: str = "info") -> None:
        tag = level if level in LOG_COLORS else "info"
        stamp = datetime.now().strftime("%H:%M:%S")
        log_list.controls.append(
            ft.Text(
                f"[{stamp}] {message}",
                size=12,
                color=LOG_COLORS[tag],
                font_family="monospace",
                selectable=True,
            )
        )

    def _apply_progress(processed: int, total: int, errors: int) -> None:
        progress_bar.value = (processed / total) if total else 0
        status_text.value = f"Обработано {processed} / {total} · ошибок: {errors}"

    async def flush_ui(*, force: bool = False) -> None:
        """Apply buffered log/progress and call page.update once (event loop only)."""
        nonlocal pending_logs, pending_progress, flush_task
        flush_task = None
        if not force and not pending_logs and pending_progress is None:
            return

        if pending_logs:
            for msg, level in pending_logs:
                _append_log_control(msg, level)
            pending_logs = []

        if pending_progress is not None:
            _apply_progress(*pending_progress)
            pending_progress = None

        page.update()

    def schedule_flush() -> None:
        nonlocal flush_task
        if flush_task is None or flush_task.done():
            flush_task = loop.create_task(_delayed_flush())

    async def _delayed_flush() -> None:
        await asyncio.sleep(UI_BATCH_MS / 1000)
        await flush_ui()

    def enqueue_log(message: str, level: str = "info") -> None:
        """Thread-safe: hop log onto the asyncio loop (batched)."""

        def _enqueue() -> None:
            pending_logs.append((message, level))
            schedule_flush()

        loop.call_soon_threadsafe(_enqueue)

    def enqueue_progress(processed: int, total: int, errors: int) -> None:
        """Thread-safe: hop progress onto the asyncio loop (coalesced)."""

        def _enqueue() -> None:
            nonlocal pending_progress
            pending_progress = (processed, total, errors)
            schedule_flush()

        loop.call_soon_threadsafe(_enqueue)

    def append_log(message: str, level: str = "info") -> None:
        """Immediate log from the UI/event-loop thread."""
        _append_log_control(message, level)
        page.update()

    def refresh_pills() -> None:
        if source_path:
            pill_source.content = ft.Text(
                f"Source · {Path(source_path).name}", size=12, color=OK
            )
            pill_source.bgcolor = PILL_OK_BG
        else:
            pill_source.content = ft.Text("Source · не выбран", size=12, color=MUTED)
            pill_source.bgcolor = PILL_MUTE_BG

        if dest_path:
            pill_dest.content = ft.Text(
                f"Dest · {Path(dest_path).name}", size=12, color=OK
            )
            pill_dest.bgcolor = PILL_OK_BG
        else:
            pill_dest.content = ft.Text("Dest · не выбран", size=12, color=MUTED)
            pill_dest.bgcolor = PILL_MUTE_BG

    def update_start_enabled() -> None:
        start_btn.disabled = not (source_path and dest_path and not is_sorting)
        page.update()

    def set_controls_busy(busy: bool) -> None:
        browse_dest_btn.disabled = busy
        browse_source_btn.disabled = busy
        clear_btn.disabled = busy
        save_log_btn.disabled = busy
        drop_zone.disabled = busy
        drop_target.disabled = busy
        dest_field.disabled = busy
        source_field.disabled = busy
        cancel_btn.disabled = not busy
        if busy:
            start_btn.disabled = True
        else:
            start_btn.disabled = not (source_path and dest_path)
        page.update()

    def set_source(path: str, *, log: bool = True) -> None:
        nonlocal source_path
        source_path = path.strip()
        p = Path(source_path)
        source_field.value = source_path
        drop_title.value = p.name or source_path
        drop_detail.value = shorten_path(source_path, 72)
        drop_zone.bgcolor = ACCENT_SOFT
        drop_zone.border = ft.Border.all(2, ACCENT)
        if log:
            append_log(f"Выбрана исходная папка: {source_path}", "info")
        refresh_pills()
        update_start_enabled()

    def set_dest(path: str, *, log: bool = True) -> None:
        nonlocal dest_path
        dest_path = path.strip()
        dest_field.value = dest_path
        if log:
            append_log(f"Папка сохранения: {dest_path}", "info")
        refresh_pills()
        update_start_enabled()

    def apply_source_field(_e=None) -> None:
        value = (source_field.value or "").strip()
        if not value:
            return
        if Path(value).is_dir():
            set_source(value)
        else:
            show_dialog("Предупреждение", "Указанный путь исходной папки не существует")

    def apply_dest_field(_e=None) -> None:
        value = (dest_field.value or "").strip()
        if not value:
            return
        parent = Path(value)
        if parent.exists() and not parent.is_dir():
            show_dialog(
                "Предупреждение",
                "Путь сохранения указывает на файл, а не папку",
            )
            return
        set_dest(value)

    async def pick_source(_e=None) -> None:
        if is_sorting:
            return
        path = await pick_directory("Выберите папку с фотографиями")
        if path:
            set_source(path)
        elif not shutil.which("zenity"):
            show_dialog(
                "Выбор папки",
                "Системный диалог недоступен (нужен zenity).\n"
                "Вставьте путь к папке в поле «Исходная папка» вручную.",
            )

    async def pick_dest(_e=None) -> None:
        if is_sorting:
            return
        path = await pick_directory("Выберите папку для сохранения")
        if path:
            set_dest(path)
        elif not shutil.which("zenity"):
            show_dialog(
                "Выбор папки",
                "Системный диалог недоступен (нужен zenity).\n"
                "Вставьте путь в поле «Папка сохранения» вручную.",
            )

    def clear_log(_e=None) -> None:
        log_list.controls.clear()
        page.update()

    def save_log(_e=None) -> None:
        nonlocal pending_logs
        dest = (dest_field.value or dest_path or "").strip()
        if not dest:
            show_dialog(
                "Предупреждение",
                "Сначала укажите папку сохранения для фотографий",
            )
            return

        dest_dir = Path(dest)
        if pending_logs:
            for msg, level in pending_logs:
                _append_log_control(msg, level)
            pending_logs = []

        lines = [
            ctrl.value
            for ctrl in log_list.controls
            if isinstance(ctrl, ft.Text) and ctrl.value
        ]
        if not lines:
            show_dialog("Предупреждение", "Лог пуст — нечего сохранять")
            return

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_path = dest_dir / f"photosorter_{stamp}.log"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            show_dialog("Ошибка", f"Не удалось сохранить лог:\n{exc}")
            return

        append_log(f"Лог сохранён: {out_path}", "ok")
        show_dialog(
            "Лог сохранён",
            f"Лог сохранён в выбранную директорию:\n{dest_dir}",
        )

    def request_cancel(_e=None) -> None:
        if not is_sorting:
            return
        cancel_event.set()
        append_log("Отмена запрошена…", "warn")
        status_text.value = "Отмена…"
        cancel_btn.disabled = True
        page.update()

    def on_sorting_finished(
        success: bool, processed: int, total: int, error_count: int
    ) -> None:
        nonlocal is_sorting
        was_cancelled = cancel_event.is_set()
        is_sorting = False
        set_controls_busy(False)

        if was_cancelled:
            status_text.value = (
                f"Отменено · обработано {processed} / {total} · ошибок: {error_count}"
            )
            page.update()
            show_dialog(
                "Отменено",
                f"Сортировка прервана.\nОбработано файлов: {processed} из {total}",
            )
        elif total == 0 and error_count == 0:
            status_text.value = "Файлы не найдены · ошибок: 0"
            page.update()
            show_dialog("Предупреждение", "Не найдено поддерживаемых файлов")
        elif success:
            progress_bar.value = 1
            status_text.value = (
                f"Готово · обработано {processed} / {total} · ошибок: 0"
            )
            page.update()
            show_dialog(
                "Готово",
                f"Сортировка завершена.\nОбработано файлов: {processed} из {total}",
            )
        else:
            status_text.value = (
                f"Завершено · обработано {processed} / {total} · ошибок: {error_count}"
            )
            page.update()
            show_dialog(
                "Внимание",
                f"Сортировка завершена с ошибками.\n"
                f"Обработано: {processed} из {total}\n"
                f"Ошибок: {error_count}",
            )

    async def start_sorting(_e=None) -> None:
        nonlocal is_sorting, source_path, dest_path
        if is_sorting:
            show_dialog("Информация", "Сортировка уже выполняется")
            return

        source_path = (source_field.value or "").strip()
        dest_path = (dest_field.value or "").strip()

        if not source_path:
            show_dialog("Предупреждение", "Выберите папку с фотографиями")
            return
        if not dest_path:
            show_dialog("Предупреждение", "Выберите папку для сохранения")
            return

        src = Path(source_path)
        dst = Path(dest_path)
        if not src.is_dir():
            show_dialog("Предупреждение", "Исходная папка не существует")
            return
        if paths_overlap(src, dst):
            show_dialog(
                "Ошибка",
                "Папка сохранения не должна совпадать с исходной "
                "и не должна быть внутри неё (и наоборот).",
            )
            return

        cancel_event.clear()
        is_sorting = True
        set_controls_busy(True)
        progress_bar.value = 0
        status_text.value = "Сортировка начата… · ошибок: 0"
        append_log("Сортировка запущена", "info")

        def run_sort() -> tuple[bool, int, int, int]:
            sorter = PhotoSorter(
                on_log=enqueue_log,
                on_progress=enqueue_progress,
                cancel_check=cancel_event.is_set,
            )
            return sorter.sort_files(source_path, dest_path)

        try:
            result = await asyncio.to_thread(run_sort)
        except Exception as exc:
            await flush_ui(force=True)
            is_sorting = False
            set_controls_busy(False)
            append_log(f"Критическая ошибка: {exc}", "error")
            show_dialog("Ошибка", f"Сортировка прервалась:\n{exc}")
            return

        await flush_ui(force=True)
        on_sorting_finished(*result)

    browse_source_btn.on_click = pick_source
    browse_dest_btn.on_click = pick_dest
    drop_zone.on_click = pick_source
    start_btn.on_click = start_sorting
    cancel_btn.on_click = request_cancel
    clear_btn.on_click = clear_log
    save_log_btn.on_click = save_log
    source_field.on_submit = apply_source_field
    dest_field.on_submit = apply_dest_field
    source_field.on_blur = apply_source_field
    dest_field.on_blur = apply_dest_field

    def path_card(title: str, field: ft.TextField, button: ft.OutlinedButton):
        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(title, size=13, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Row(
                        [field, button],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=12,
            padding=16,
            expand=True,
        )

    page.add(
        ft.Column(
            [
                ft.Text("Photo Sorter", size=28, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Text(
                    "Год съёмки → устройство → имя по дате изменения файла",
                    size=13,
                    color=MUTED,
                ),
                ft.Container(height=8),
                ft.Row(
                    [
                        path_card("Папка сохранения", dest_field, browse_dest_btn),
                        path_card("Исходная папка", source_field, browse_source_btn),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                ft.Container(height=4),
                drop_target,
                ft.Text(
                    f"Без устройства или при сбое файлы попадут в "
                    f"год / {UNSORTED_DIR_NAME}",
                    size=12,
                    color=MUTED,
                ),
                ft.Row([pill_source, pill_dest, pill_video], spacing=8, wrap=True),
                ft.Container(height=4),
                progress_bar,
                status_text,
                ft.Row(
                    [start_btn, cancel_btn, save_log_btn, clear_btn],
                    spacing=10,
                    wrap=True,
                ),
                ft.Text(
                    "Лог операций",
                    size=13,
                    weight=ft.FontWeight.BOLD,
                    color=TEXT,
                ),
                log_panel,
            ],
            expand=True,
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
        )
    )

    if not has_ffprobe:
        append_log(
            "ffprobe не найден — метаданные видео будут браться "
            "из времени изменения файла",
            "warn",
        )


if __name__ == "__main__":
    ft.run(main)
