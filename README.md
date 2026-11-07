# `penny-win-worker` — Windows CAD/CAE Execution Engine for Penny COS Bot

Dedicated Windows HTTP Worker microservice that isolates Windows-only desktop engineering software (**CSI SAP2000 v25 OAPI** and **AutoCAD COM / ODA DWG Title Block Automation**) from the main Linux `Penny_COS_Bot` container.

---

## Architecture

```
[ Penny_COS_Bot (Linux VM / Docker) ]
        │
        │  HTTP + Bearer Token (Tailscale / LAN :8085)
        ▼
[ penny-win-worker (Windows VM / Workstation) ]
  ├── GET  /health
  ├── POST /analyze                  (SAP2000 v25 OAPI structural analysis + EC3 design)
  ├── POST /sap2000/analyze          (Alias for /analyze)
  ├── POST /sap2000/build-portal     (Parametric 2D EC3 Steel Portal Frame builder)
  └── POST /autocad/update-title-block (Native AutoCAD COM + ODA/ezdxf DWG updater)
```

### Key Reliability Features
1. **COM Serialization Lock:** Ensures only one SAP2000 or AutoCAD COM operation runs at a time (`_SAP_LOCK` / `_CAD_LOCK`).
2. **Process Watchdog (`engines/process_guard.py`):** Wraps every COM execution in a strict timeout and automatically runs `taskkill /F /T /IM SAP2000.exe` (or `acad.exe`) if a singular stiffness matrix or modal popup hangs the engine.
3. **Two-Tier DWG Engine (`engines/autocad_engine.py`):**
   - **Tier 1 (Native AutoCAD COM):** Uses `win32com.client.Dispatch("AutoCAD.Application")` to open `.dwg` files directly, update Block Reference Attributes (`AcDbBlockReference`), `AcDbMText`, and `AcDbText` across ModelSpace and PaperSpace Layouts, and save natively.
   - **Tier 2 (ODA File Converter + `ezdxf`):** Falls back to `ODAFileConverter.exe` + `ezdxf` with full round-trip verification if AutoCAD is not installed on the Windows machine.

---

## Quick Start on Windows VM

### 1. Install Dependencies
```powershell
git clone https://github.com/harry1835/penny-win-worker.git
cd penny-win-worker
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run the Worker
```powershell
python worker_server.py --host 0.0.0.0 --port 8085 --token YOUR_SECRET_TOKEN
```

### 3. Register Auto-Start on Windows Login (Interactive Session)
Because both SAP2000 OAPI and AutoCAD COM require an interactive GUI session (and will fail with `CO_E_SERVER_EXEC_FAILURE` if run inside a headless Session 0 Windows Service), use the included PowerShell script to register an **Interactive Scheduled Task**:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_task.ps1
```
### 4. Update and Restart
After `main` changes, pull and restart in one go (waits for the old run to end, then checks `/health`):
```powershell
.\scripts\restart_worker.ps1          # add -NoPull to restart without pulling
```
If PowerShell blocks scripts: `powershell -ExecutionPolicy Bypass -File .\scripts\restart_worker.ps1`.

> **Tip when using Remote Desktop (RDP):** Always disconnect from the Windows VM by running `scripts\disconnect_rdp_keep_gui.bat` as Administrator instead of clicking the RDP close (`X`) button. This hands the session back to the local console (`tscon`) so the GUI surface remains active for SAP2000 and AutoCAD.

---

## Connecting `Penny_COS_Bot` (Linux)

In `Penny_COS_Bot/.env` on your Linux VM:
```ini
WIN_WORKER_URL=http://<windows-tailscale-or-lan-ip>:8085
WIN_WORKER_TOKEN=YOUR_SECRET_TOKEN
# Legacy aliases also supported automatically:
SAP2000_WORKER_URL=http://<windows-tailscale-or-lan-ip>:8085
SAP2000_WORKER_TOKEN=YOUR_SECRET_TOKEN
```

---

## Running Tests
```bash
pytest -v
# or
python -m unittest discover -s tests -v
```
