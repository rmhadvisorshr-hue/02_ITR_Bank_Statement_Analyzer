# Bank Statement Analyzer

Local web app for converting bank statement PDFs into an Excel analysis workbook.

## What It Does

- Upload one or more bank statement PDFs from the browser.
- Supports many Indian bank statement layouts, including HDFC, Axis, IDFC First, IDBI, ICICI, DCB, Bank of Baroda, Bank of Maharashtra, Union Bank, Federal/Kotak-style layouts, and cooperative-bank layouts present in the sample data.
- Supports password-protected statements by entering a password before generating.
- Automatically reads statement passwords when the password is included in the filename, such as `PSW-76681350` or `PW-AMBI909233582`.
- Generates an Excel workbook with:
  - Total receipts and payments
  - Cash
  - Recurring transactions
  - EMI
  - Salary
  - High transactions
  - UPI
  - LIC
  - FD interest
  - FD deposits and withdrawals
  - PPF/PF interest and contribution
  - Monthly summary
  - Transaction detail
  - Checks and source sheets

## How To Run

The backend is a single Python/FastAPI process (`analysis_service/main.py`). It serves the API, and - for the Excel-generation step only - shells out to a small Node CLI tool (`build_hdfc_workbook_cli.mjs`); Node never runs as its own server.

### Option A - single server (simplest)

1. Open a terminal in `ITRBankStatementAnalyzer\backend`.

2. Install dependencies (once):

   `pip install -r requirements.txt`
   `npm install` (only pulls in `exceljs`, used by the CLI workbook-generation step)

3. Start the server:

   `python -m uvicorn analysis_service.main:app --reload --host 0.0.0.0 --port 8765`
   `python -m uvicorn analysis_service.main:app --host 0.0.0.0 --port 8765`

   On Windows, drop `--reload`: uvicorn forces the server process onto
   `WindowsSelectorEventLoopPolicy` whenever `--reload` (or `--workers > 1`)
   is used, and that loop can't spawn subprocesses on Windows -
   `pipeline.py`'s `asyncio.create_subprocess_exec` calls (to run
   `extract_bank.py` and the Node workbook builder) fail with
   `NotImplementedError`. Linux/macOS are unaffected.

4. Open:

   `http://127.0.0.1:8765/`

This one command is everything: it serves the frontend, the API, and (when `REDIS_URL` is set) the arq worker sidecars. Nothing else in the monorepo is started.

### Option B - frontend and backend as separate processes

Use this if you want to edit/reload the frontend independently of the backend.

**Terminal 1 - backend** (`ITRBankStatementAnalyzer\backend`): the command above. Set `FRONTEND_DEV_URL=http://localhost:5500` in `backend\.env` (already set by default) so it accepts requests from the standalone frontend below.

**Terminal 2 - frontend** (`ITRBankStatementAnalyzer\frontend`):

`npm install` (no-op, no dependencies) then `npm run dev`

Starts a static file server on `http://localhost:5500` for `frontend\public` only - it never starts Python or any other project in the monorepo. It talks to the backend over HTTP using `BACKEND_URL` (defaults to `http://localhost:8765`, override with `BACKEND_URL=... npm run dev` if the backend runs elsewhere).

### Using either option

1. Open the frontend URL for whichever option you used.
2. Drop/select PDF bank statements.
3. If the statements are password protected, enter the statement password.
4. Click **Generate Excel**.
5. Download the generated workbook.

## Project Structure

- `frontend\public\index.html` - frontend page
- `frontend\public\styles.css` - frontend styling
- `frontend\public\app.js` - browser upload and download logic
- `frontend\package.json` / `frontend\dev-server.mjs` - optional standalone static server for the frontend alone (Option B above); no build step, no dependencies for local dev. `frontend\build.mjs` does a one-time `{{BACKEND_URL}}` substitution for the Vercel production build only.
- `backend\requirements.txt` - Python dependencies (includes FastAPI/uvicorn - this is the entire backend runtime)
- `backend\.env` / `backend\.env.example` - environment configuration (see `.env.example` for what each variable does)
- `backend\analysis_service\main.py` - the FastAPI app: routing, uploads, CORS, static frontend serving, and orchestration
- `backend\analysis_service\pipeline.py` - the extract → build → chart pipeline
- `backend\extract_bank.py` - PDF extraction and bank parsing logic
- `backend\db.py` - SQLite report-metadata store
- `backend\storage\r2.py` - Cloudflare R2 (S3-compatible) report storage
- `backend\cleanup.py` - stale job-directory sweep
- `backend\analysis_worker\` - arq worker for Redis-backed job queuing (dormant unless `REDIS_URL` is set); spawned by `analysis_service/main.py`'s lifespan
- `backend\package.json` - the *only* Node dependency left (`exceljs`); `backend\build_hdfc_workbook.mjs` / `backend\build_hdfc_workbook_cli.mjs` generate the Excel workbook and are invoked by `pipeline.py` as a CLI subprocess - Node never runs as its own server
- `docs\knowledge-base.html` - engineering notes (not loaded by the app at runtime; gitignored, excluded from deploys)
- `outputs\` - generated Excel files

## Notes

- This is a BETA app. Your PDFs are processed on your own computer.
- If port `8765` is already busy, stop the old server process before starting a new one.
