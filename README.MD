# Photo Sorter

## Windows build

The Windows app must be built on a Windows runner. The GitHub Actions workflow
**Build Windows EXE** runs on pushes to `main`/`master` and can also be started
manually from the Actions tab with **Run workflow**.

After a successful run, download the `PhotoSorter-Windows-x64` artifact and
extract the entire folder. Run `photosorter.exe` from that folder; the Flutter
runtime DLLs and `data` directory must stay alongside the executable.

The build workflow runs the Python tests and verifies that Flutter registered
the Windows `desktop_drop` plugin used by `flet_dropzone` before publishing the
bundle.
