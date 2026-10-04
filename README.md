# Disk Space Map

A live dashboard that shows what's using the space on your Windows drives, and updates the moment files change.

It scans your drives once, then listens to Windows file-change notifications (`ReadDirectoryChangesW`) and re-reads only the folders that changed, usually within a second. No rescanning everything every few minutes.

## What you get

- **Drives**: used and free space, updated every 2 seconds, plus how much it changed since you opened the page.
- **Live activity**: every folder that grows or shrinks by 1 MB or more, as it happens.
- **Folder explorer**: a zoomable treemap with a sorted folder list. Click to drill in, and color by folder or by last change. Blocks flash when their size changes.
- **Largest files**: every file of 100 MB or more, with search and filters for drive, type and age.
- **File types and age**: what kind of data you have, and how old it is.
- **Cleanup candidates**: caches, temp folders, `node_modules`, Python venvs, package caches, the Recycle Bin, game libraries.
- **Large and untouched**: big folders nothing has changed in for two years.
- **Possible duplicates**: large files with the same name and size in more than one place.
- **Rescan everything**: a full rescan with a progress bar, for when you want exact numbers again.

## Requirements

- Windows 10 or 11
- Python 3.12 or newer. It uses only the standard library, so there's nothing to install.

## Run it

Double-click `Start Disk Space Map.bat`, or run:

```
python server.py
```

Your browser opens `http://127.0.0.1:8765`. The first scan takes a minute or two, depending on how many files you have. Close the minimized **Disk Space Map** window (or press Ctrl+C) to stop it.

To scan other drives, edit `DRIVES` near the top of `server.py`.

## Privacy and safety

- **Read-only.** It never changes, moves or deletes anything on disk.
- **Local only.** The server listens on `127.0.0.1`, so other computers can't reach it. It also rejects requests from other websites (it checks the Host header, and needs a custom header to start a rescan).
- **Nothing saved.** Scan results live in memory only, and nothing is written to disk or sent anywhere. The page loads fonts from Google Fonts and the d3 library from cdnjs.

## Notes

- Folders your account can't open, such as restore points and other users' files, are skipped. The drive bar shows them as "couldn't read".
- Online-only cloud files (Dropbox, OneDrive placeholders) don't use disk space and aren't counted.
- Under very heavy disk activity Windows can drop some change notifications. The page shows a warning when that happens, and **Rescan everything** fixes it.
- File types and the by-year chart come from the last full scan. Everything else updates live.
- Memory use is roughly 100 MB per million files indexed.

## License

MIT. See [LICENSE](LICENSE).
