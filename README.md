# OpsCenter

OpsCenter is a lightweight, local single-user web application designed for operations teams. It features a fast-triage **Inbox** for dropping notes/screenshots/files, an interactive **Kanban Board** with workdays-based card-aging tracking, a **Hang Detector** with escalation management via the "Ping Today" panel, full-text search (FTS5), dynamic statistics, and automated backup capabilities.


---

## 🛠️ Tech Stack & Key Features

- **Backend:** Python 3.11+, FastAPI (with native Uvicorn runner)
- **Database:** SQLite (WAL mode, FTS5 tokenization)
- **Frontend:** Jinja2 templates, Vanilla JavaScript & CSS (completely local, zero external network requests/CDNs)
- **Key Features:**
  - Fast-triage keyboard navigation (hotkeys for archiving, deleting, pinning, and attaching notes).
  - Drag-and-drop or paste (Ctrl+V) file buffering in the Inbox.
  - Gemini LLM-powered note triage suggestions and image OCR text extraction.
  - Automatic workdays calculator (excluding weekends) for precise stage aging.
  - Robust backup generation using SQL `VACUUM INTO`.

---

## Important: Python on Windows Target Machines

On target Windows systems, the bare `python` and `pythonw` commands are often **Windows Store execution aliases** (blank stubs) that do not point to a real interpreter.

To prevent execution failures, **always use the absolute path to the virtual environment interpreter** in your scripts, Task Scheduler tasks, or shortcuts:
- `.venv\Scripts\python.exe` — Console-enabled interpreter (for setup, tests, and manual backups).
- `.venv\Scripts\pythonw.exe` — Windowless interpreter (for launching the background web service silently).

Do not rely on global `python` references. Below, `<project-path>` represents the absolute path of the directory (e.g., `D:\OPS\opscenter`).

---

## 📦 1. Installation on Windows

Run all commands inside PowerShell from the project root directory:

1. **Create the Python Virtual Environment:**
   If you have the Python launcher installed, run:
   ```powershell
   py -3 -m venv .venv
   ```
   If `py` is not found, use the direct path to your Python installation.

2. **Install Project Dependencies:**
   Install required packages using the venv pip package manager:
   ```powershell
   .venv\Scripts\python.exe -m pip install --upgrade pip
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. **Activate the Environment (Optional for current session):**
   ```powershell
   .venv\Scripts\Activate.ps1
   ```
   *Note: While activation works for manual terminal operations, Task Scheduler jobs and Windows shortcuts must always define the direct path to `.venv\Scripts\...`.*

---

## 🚀 2. Running the Application

Start the local server using the following command:
```powershell
.venv\Scripts\python.exe run.py
```
This runs the Uvicorn web server on `http://127.0.0.1:8765/` and automatically launches your default web browser to that address. Press `Ctrl+C` in the console window to stop the server.

### Storage Directory
By default, the application creates a `data/` directory (for SQLite files, attachments, and backups) inside the project folder. You can override this location by setting the `OPSCENTER_DATA_DIR` environment variable.

---

## ⏰ 3. Windows Task Scheduler Auto-Start

To run the application silently in the background when logging into Windows:

1. Open the **Windows Task Scheduler** (`taskschd.msc`).
2. Create a new Task with the following settings:
   - **Trigger:** "At log on" (or "At startup").
   - **Action:** "Start a program".
     - **Program/script:** `<project-path>\.venv\Scripts\pythonw.exe` (silent executor).
     - **Add arguments:** `run.py`
     - **Start in:** `<project-path>` (the absolute path of the project is required so modules can be imported correctly).

#### Configuration Example (for path `D:\OPS\opscenter`):
| Field | Value |
| --- | --- |
| **Program/script** | `D:\OPS\opscenter\.venv\Scripts\pythonw.exe` |
| **Add arguments** | `run.py` |
| **Start in** | `D:\OPS\opscenter` |

---

## 💾 4. Automated Daily Backups

The `backup.py` script performs incremental generation backups:
- Creates a safe database clone using **`VACUUM INTO`**.
- Copies the `attachments/` folder.
- Keeps the **7 most recent generations** inside `data\backups\`, deleting older ones to save disk space.

> 🚨 **WARNING:** Do not copy `data\opscenter.db` directly while the application is running. Because of SQLite's WAL (Write-Ahead Logging) mode, active changes live in `-wal` and `-shm` side-files. A direct copy will result in a corrupted or incomplete database. Always use `backup.py` for live backups.

### Running a Backup manually:
```powershell
.venv\Scripts\python.exe backup.py
```

### Windows Task Scheduler Backup Job:
- **Trigger:** Daily (e.g., at night).
- **Action:** "Start a program".
  - **Program/script:** `<project-path>\.venv\Scripts\python.exe`
  - **Add arguments:** `backup.py`
  - **Start in:** `<project-path>`

---

## 🔄 5. Restoring from a Backup

Each backup generation is stored inside `data\backups\<generation>\` (where `<generation>` is formatted as `YYYY-MM-DD_HHMMSS`) and contains `opscenter.db` and the `attachments/` directory.

### Restore Steps:
1. **Stop the Application** completely (close the `run.py` terminal or stop the Task Scheduler service).
2. **Back up current state:** Copy your current `data/` directory to a safe location before overwriting anything.
3. **Restore Database:** Copy `data\backups\<generation>\opscenter.db` into `data\opscenter.db` (overwriting the active file). Delete or rename any lingering `opscenter.db-wal` or `opscenter.db-shm` files to avoid matching state errors.
4. **Restore Attachments:** Replace the current `data\attachments\` folder with the contents of `data\backups\<generation>\attachments\`.
5. **Restart the Application** and verify that your tasks, notes, and file attachments are visible.

---

## 🧪 Running Unit Tests

Run the test suite using `pytest`:
```powershell
.venv\Scripts\python.exe -m pytest -q
```

---

## 📄 License

Released under the [MIT License](LICENSE).
