from pathlib import Path


def test_main_uses_flet_dropzone_for_drag_and_drop() -> None:
    source = Path(__file__).resolve().parents[1] / "main.py"
    text = source.read_text(encoding="utf-8")

    assert "import flet_dropzone" in text
    assert "ftd.Dropzone" in text
