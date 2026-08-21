import argparse
import csv
import json
import os
import pathlib
import re
import sys
import concurrent.futures

# Ensure the app directory is in sys.path for worker processes
sys.path.insert(0, str(pathlib.Path(__file__).parent.resolve()))

import pdfplumber

from parser.generic_parser import (
    to_float,
    find_period,
    extract_opening_balance,
    extract_grand_totals,
    extract_account_details,
    classify,
)
from parser.hdfc_parser import build_hdfc_transactions, build_hdfc_loan_transactions
from parser.parser_selector import detect_bank, get_transaction_builder
from parser.icici_parser import build_icici_od_transactions, is_icici_od_detailed_statement


def slugify_name(name):
    name = name.upper()
    for prefix in ["M/S.", "M/S", "MR.", "MRS.", "MR", "MRS", "DR.", "DR", "MISS", "MS.", "MS"]:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
    name = re.sub(r"[^A-Z0-9]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "ACCOUNT_HOLDER"


def format_period_slug(date_str):
    if date_str and len(date_str) >= 7:
        return date_str[:7].replace("-", "_")
    return "UNKNOWN"


def password_candidates(path, manual_passwords=None):
    stem = pathlib.Path(path).stem
    candidates = [""]
    for password in manual_passwords or []:
        password = str(password).strip()
        if password:
            candidates.append(password)
    for pattern in [
        r"PW[-\s]*([A-Za-z0-9]+)",
        r"PWD[-\s]*([A-Za-z0-9]+)",
        r"PSW[-\s]*([A-Za-z0-9]+)",
        r"PWS[-\s]*([A-Za-z0-9]+)",
        r"-\s*([A-Z]{4}\d{4})$",
    ]:
        match = re.search(pattern, stem, re.I)
        if match:
            candidates.append(match.group(1))
    return list(dict.fromkeys(candidates))


def _looks_like_passbook_ledger(text):
    """Heuristic for the "Date | Particulars | Cheque No. | Withdrawals |
    Deposits | Balance" physical-passbook table, as opposed to any other
    statement layout. Deliberately only consulted when no bank could be
    identified at all - a recognised bank's OCR'd statement is left to
    its own bank-specific handling (or the generic fallback) regardless
    of whether it happens to share some of these column names too."""
    header_zone = text[:3000].lower()
    required = ("withdrawal", "deposit", "particulars", "balance")
    return all(term in header_zone for term in required)


def read_pdf_text(path, manual_passwords=None):
    last_error = None
    for password in password_candidates(path, manual_passwords):
        try:
            kwargs = {"password": password} if password else {}
            with pdfplumber.open(path, **kwargs) as pdf:
                text = "\n".join(page.extract_text(x_tolerance=1, y_tolerance=3) or "" for page in pdf.pages)
                return text, len(pdf.pages), password
        except Exception as exc:
            last_error = exc
    raise last_error


def read_pdf_page_words(path, password):
    """Extract positioned words for layouts whose columns wrap independently."""
    kwargs = {"password": password} if password else {}
    with pdfplumber.open(path, **kwargs) as pdf:
        return [
            page.extract_words(x_tolerance=1, y_tolerance=2, use_text_flow=False)
            for page in pdf.pages
        ]


def process_single_pdf(pdf_path, manual_passwords, use_ocr=False, auto_ocr=False):
    try:
        try:
            from ocr import extract_text_with_ocr, is_scanned_pdf
            ocr_available = True
        except ImportError as e:
            ocr_available = False
            ocr_import_error = str(e)

        is_ocr_text = False
        if use_ocr:
            if not ocr_available:
                return {"success": False, "file": pdf_path.name, "issue": f"OCR dependencies are missing. Please install pytesseract and pdf2image. Details: {ocr_import_error}", "bank": "Unknown"}
            text, pages, password = extract_text_with_ocr(pdf_path, manual_passwords)
            is_ocr_text = True
        else:
            text, pages, password = read_pdf_text(pdf_path, manual_passwords)
            if auto_ocr and ocr_available:
                # `pages == 0` means pdfplumber couldn't even walk this
                # PDF's page tree (seen on statements a different tool
                # generated/repaired) - that's not "no text", it's "normal
                # extraction can't read this file at all", so it needs OCR
                # just as much as a page pdfplumber can open but that has
                # no text layer. pdf2image/poppler parses the page tree
                # independently of pdfplumber, so it can still succeed here.
                if pages == 0 or is_scanned_pdf(text, pages):
                    print(json.dumps({"type": "progress", "message": f"Scanned PDF detected for {pdf_path.name}. Using OCR extraction."}), flush=True)
                    text, pages, password = extract_text_with_ocr(pdf_path, manual_passwords)
                    is_ocr_text = True

        bank = detect_bank(text, pdf_path.name)
        print(json.dumps({"type": "progress", "message": f"DEBUG: Detected {bank} for {pdf_path.name}"}), flush=True)
        st_from, st_to = find_period(text)
        opening = extract_opening_balance(text)
        grand_totals = extract_grand_totals(text)

        issues = []
        if bank == "HDFC Bank":
            if is_ocr_text:
                from adapters.hdfc_ocr_adapter import build_hdfc_ocr_transactions
                file_records = build_hdfc_ocr_transactions(text, pdf_path.name, bank, st_from, st_to, opening, 0, issues)
            elif "Loan Type" in text and "PERSONAL LOAN" in text:
                file_records = build_hdfc_loan_transactions(text, pdf_path.name, bank, st_from, st_to, opening, 0)
            else:
                file_records = build_hdfc_transactions(text, pdf_path.name, bank, st_from, st_to, opening, 0, issues)
        elif bank == "ICICI Bank" and not is_ocr_text and is_icici_od_detailed_statement(text):
            # ICICI's current/OD Detailed Statement has independently wrapped
            # columns.  Its flat text stream loses column ownership, so parse
            # positioned PDF words rather than sending it through any generic
            # date/amount heuristic.
            page_words = read_pdf_page_words(pdf_path, password)
            file_records = build_icici_od_transactions(
                page_words, pdf_path.name, bank, st_from, st_to, opening, 0
            )
        elif is_ocr_text and bank == "Unknown Bank" and _looks_like_passbook_ledger(text):
            # No bank name/logo anywhere in the OCR'd text, but the
            # classic "Date | Particulars | Cheque No. | Withdrawals |
            # Deposits | Balance" passbook-ledger columns are - this is a
            # photographed physical passbook (see BANK STATEMENT.pdf /
            # BANK STATEMENT 2.pdf). Withdrawals and Deposits are two
            # separate columns distinguished only by position on the
            # page, which a flat OCR text dump throws away - re-run OCR
            # once more in bounding-box mode so the passbook adapter can
            # read the columns positionally instead of guessing from
            # keywords that this layout simply doesn't have.
            print(json.dumps({"type": "progress", "message": f"Passbook-style ledger detected for {pdf_path.name}. Re-running OCR with column positions."}), flush=True)
            from ocr import extract_words_with_boxes
            from adapters.passbook_ocr_adapter import build_passbook_transactions
            _, _, _, pages_words = extract_words_with_boxes(pdf_path, manual_passwords)
            file_records = build_passbook_transactions(pages_words, pdf_path.name, bank, st_from, st_to, opening, 0, issues)
        else:
            builder = get_transaction_builder(bank)
            file_records = builder(text, pdf_path.name, bank, st_from, st_to, opening, 0)

        import sys
        sys.stderr.write(f"DEBUG: Detected {bank} for {pdf_path.name}\n")
        print(json.dumps({"type": "progress", "message": f"DEBUG: Parsed {len(file_records)} records"}), flush=True)
        sys.stderr.write(f"DEBUG: Parsed {len(file_records)} records\n")
        if not file_records:
            return {"success": False, "file": pdf_path.name, "issue": "no transactions parsed", "bank": bank}

        parsed_debits = round(sum(r["withdrawal"] for r in file_records), 2)
        parsed_credits = round(sum(r["deposit"] for r in file_records), 2)
        acc_name, acc_no = extract_account_details(text)

        statement = {
            "file": pdf_path.name,
            "pages": pages,
            "from": st_from or min(r["date"] for r in file_records),
            "to": st_to or max(r["date"] for r in file_records),
            "opening_balance": to_float(opening) if opening is not None else 0,
            "dr_count": sum(1 for r in file_records if r["withdrawal"] != 0),
            "cr_count": sum(1 for r in file_records if r["deposit"] != 0),
            "debits": to_float(grand_totals["debits"]) if grand_totals else parsed_debits,
            "credits": to_float(grand_totals["credits"]) if grand_totals else parsed_credits,
            "closing_balance": to_float(grand_totals["closing_balance"]) if grand_totals else file_records[-1]["closing_balance"],
            "bank": bank,
            "password_used": bool(password),
            "account_name": acc_name,
            "account_no": acc_no,
        }
        return {"success": True, "file": pdf_path.name, "records": file_records, "statement": statement, "issues": issues}
    except Exception as exc:
        return {"success": False, "file": pdf_path.name, "issue": f"could not read/parse PDF: {type(exc).__name__}"}

def extract(input_dir, work_dir, manual_passwords=None, use_ocr=False, auto_ocr=False):
    pdfs = sorted(pathlib.Path(input_dir).glob("*.pdf"))
    if not pdfs:
        raise ValueError("No PDF files were uploaded.")

    records = []
    statements = []
    issues = []

    # Process PDFs in parallel to massively speed up multi-file extraction.
    # Capped rather than defaulting to os.cpu_count(): OCR'd pages are
    # rasterised as full images and handed to a tesseract subprocess, and
    # several of those happening at once (one per worker) is what was
    # driving the process pool to exhaust memory outright on a scanned,
    # many-page statement - a crash `process_single_pdf`'s own try/except
    # can't catch, since the worker process itself dies. Capping keeps
    # peak concurrent memory bounded regardless of how many CPUs the
    # machine has.
    max_workers = min(4, os.cpu_count() or 4)
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_single_pdf, pdf_path, manual_passwords, use_ocr, auto_ocr): pdf_path for pdf_path in pdfs}

        # We need to maintain sequential order based on filenames for predictable sequence IDs later
        results_by_file = {}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            pdf_path = futures[future]
            try:
                results_by_file[pdf_path.name] = future.result()
            except Exception as exc:
                # A worker process can die outright (out of memory
                # rasterising a large scanned PDF at high DPI, for
                # instance) rather than raising a normal Python exception
                # inside process_single_pdf's own try/except - that comes
                # back here as BrokenProcessPool instead. Left unguarded,
                # *one* such crash takes future.result() down, which
                # aborts this whole loop and every other PDF's
                # already-finished results with it. One bad file should
                # never fail the entire batch.
                results_by_file[pdf_path.name] = {
                    "success": False,
                    "file": pdf_path.name,
                    "issue": f"processing crashed: {type(exc).__name__}: {exc}",
                    "bank": "Unknown",
                }
            print(json.dumps({"type": "progress", "message": f"Extracting data from PDFs...", "pdf_progress": {"current": i, "total": len(pdfs)}}), flush=True)

    seq = 0
    for pdf_path in pdfs:
        res = results_by_file[pdf_path.name]
        if not res["success"]:
            issues.append({"file": res["file"], "issue": res["issue"], "bank": res.get("bank", "Unknown")})
            continue

        file_records = res["records"]
        # Update sequence numbers sequentially to maintain order
        for r in file_records:
            seq += 1
            r["seq"] = seq

        statements.append(res["statement"])
        records.extend(file_records)
        issues.extend(res.get("issues", []))

    classify(records)
    records.sort(key=lambda row: (row["date"], row["seq"]))

    work_path = pathlib.Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "seq",
        "source_file",
        "statement_from",
        "statement_to",
        "date",
        "value_date",
        "narration",
        "reference",
        "direction",
        "withdrawal",
        "deposit",
        "amount",
        "closing_balance",
        "categories",
        "is_recurring",
        "recurring_key",
    ]
    with (work_path / "transactions.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    (work_path / "statements.json").write_text(json.dumps(statements, indent=2), encoding="utf-8")
    (work_path / "issues.json").write_text(json.dumps(issues, indent=2), encoding="utf-8")

    job_acc_name = ""
    job_acc_no = ""
    for s in statements:
        if s.get("account_name"):
            job_acc_name = slugify_name(s["account_name"])
            break
    for s in statements:
        if s.get("account_no"):
            job_acc_no = s["account_no"]
            break

    if not job_acc_name:
        job_acc_name = "UNKNOWN_HOLDER"
    if not job_acc_no:
        job_acc_no = "0000"

    job_last4 = job_acc_no[-4:] if len(job_acc_no) >= 4 else job_acc_no

    valid_froms = [s["from"] for s in statements if s["from"]]
    valid_tos = [s["to"] for s in statements if s["to"]]

    min_from = min(valid_froms) if valid_froms else "YYYY_MM"
    max_to = max(valid_tos) if valid_tos else "YYYY_MM"

    start_period = format_period_slug(min_from)
    end_period = format_period_slug(max_to)

    dynamic_filename = f"Analysis_{job_acc_name}_{job_last4}_{start_period}_to_{end_period}.xlsx"

    return {
        "pdfs": len(pdfs),
        "statements": len(statements),
        "transactions": len(records),
        "issues": issues,
        "banks": sorted(set(s["bank"] for s in statements)),
        "password_protected_statements": sum(1 for s in statements if s.get("password_used")),
        "total_payments": round(sum(r["withdrawal"] for r in records), 2),
        "total_receipts": round(sum(r["deposit"] for r in records), 2),
        "account_name": job_acc_name,
        "account_no_last4": job_last4,
        "start_period": start_period,
        "end_period": end_period,
        "dynamic_filename": dynamic_filename,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument(
        "--password",
        action="append",
        default=[],
        help="Optional statement password. Can be supplied more than once.",
    )
    parser.add_argument("--use-ocr", action="store_true")
    parser.add_argument("--auto-ocr", action="store_true")
    args = parser.parse_args()
    summary = extract(args.input_dir, args.work_dir, args.password, args.use_ocr, args.auto_ocr)
    pathlib.Path(args.summary_json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if summary["issues"]:
        print(json.dumps(summary, indent=2))
    else:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
