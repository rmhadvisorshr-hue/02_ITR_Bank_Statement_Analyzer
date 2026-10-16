import glob
import os
import shutil
import pytesseract
from pdf2image import convert_from_path, pdfinfo_from_path
import re
import pathlib
from .image_preprocessing import preprocess_image


def _locate_tesseract_cmd():
    """Find the tesseract executable without hardcoding one machine's layout.

    Checks (in order): whatever's already on PATH, an explicit
    TESSERACT_CMD override, then the handful of locations Windows
    installers (the official installer and winget) actually use.
    """
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    env_override = os.environ.get("TESSERACT_CMD")
    if env_override and pathlib.Path(env_override).is_file():
        return env_override
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for pattern in [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*Tesseract*\**\tesseract.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]:
        candidates.extend(glob.glob(pattern, recursive=True))
    for candidate in candidates:
        if pathlib.Path(candidate).is_file():
            return candidate
    return None


def _locate_poppler_bin_dir():
    """Find the directory containing poppler's `pdftoppm`/`pdftocairo`.

    Same resolution order as `_locate_tesseract_cmd`: PATH, then an
    explicit override, then the standard winget install layout.
    `pdf2image` accepts `poppler_path=None` to mean "use PATH", so this
    only needs to return something when poppler isn't already on PATH.
    """
    if shutil.which("pdftoppm"):
        return None
    env_override = os.environ.get("POPPLER_PATH")
    if env_override and pathlib.Path(env_override).is_dir():
        return env_override
    for pattern in [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*Poppler*\**\pdftoppm.exe"),
        r"C:\Program Files\poppler*\Library\bin\pdftoppm.exe",
        r"C:\poppler*\Library\bin\pdftoppm.exe",
    ]:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return str(pathlib.Path(matches[0]).parent)
    return None


_TESSERACT_CMD = _locate_tesseract_cmd()
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

_POPPLER_PATH = _locate_poppler_bin_dir()


def _password_candidates(path, manual_passwords):
    passwords_to_try = [""]
    if manual_passwords:
        passwords_to_try.extend([str(p).strip() for p in manual_passwords if str(p).strip()])

    stem = pathlib.Path(path).stem
    for pattern in [
        r"PW[-\s]*([A-Za-z0-9]+)",
        r"PWD[-\s]*([A-Za-z0-9]+)",
        r"PSW[-\s]*([A-Za-z0-9]+)",
        r"PWS[-\s]*([A-Za-z0-9]+)",
        r"-\s*([A-Z]{4}\d{4})$",
    ]:
        match = re.search(pattern, stem, re.I)
        if match:
            passwords_to_try.append(match.group(1))

    return list(dict.fromkeys(passwords_to_try))


# Rasterising every page of a long scanned PDF at 300dpi *up front*
# (pdf2image's default) means all of them sit decoded in memory at once -
# fine for a handful of pages, but a real problem for the 50-100+ page
# statements in this batch: several such files being OCR'd in parallel
# (one per worker process) can exhaust system memory and crash the
# worker outright, which - unlike a normal Python exception - isn't
# something a per-file try/except can catch (see extract_bank.py's
# ProcessPoolExecutor loop). Converting/OCR'ing a bounded chunk of pages
# at a time keeps peak memory roughly constant regardless of document
# length.
_PAGE_CHUNK_SIZE = 8


def _page_count(path, password):
    kwargs = {"userpw": password} if password else {}
    try:
        info = pdfinfo_from_path(str(path), poppler_path=_POPPLER_PATH, **kwargs)
        pages = info.get("Pages")
        if pages:
            return int(pages)
    except Exception:
        pass
    return None


def _iter_page_images(path, password, dpi=300):
    total = _page_count(path, password)
    kwargs = {"userpw": password} if password else {}
    if not total:
        # pdfinfo couldn't determine a page count (rare) - fall back to
        # the old all-at-once behaviour rather than yielding nothing.
        for image in convert_from_path(str(path), dpi=dpi, poppler_path=_POPPLER_PATH, **kwargs):
            yield image
        return

    start = 1
    while start <= total:
        end = min(start + _PAGE_CHUNK_SIZE - 1, total)
        chunk = convert_from_path(
            str(path), dpi=dpi, poppler_path=_POPPLER_PATH, first_page=start, last_page=end, **kwargs
        )
        for image in chunk:
            yield image
        start = end + 1


# Tesseract occasionally reads the wide letter-spacing/gridlines of a
# statement table as a spurious space it inserts *inside* a single
# printed token, rather than between two real ones. Two shapes of this
# corrupt every downstream bank parser identically (all of them tokenize
# amounts/dates with the same regexes in generic_parser.py), so it's
# fixed once here for every bank's OCR path rather than in any one
# bank-specific module:
#
#   1. Inside an Indian-format grouped number, right before one of the
#      ",ddd" groups - "6,27,532.77" comes back as "6,27 ,532.77". Once
#      that hits AMOUNT_RE, the regex only recognises the LAST group
#      ("532.77") as a complete token; the "6,27" in front of the space
#      isn't attached to anything and is silently dropped, so a real
#      balance of 6,27,532.77 is parsed as 532.77 - which then corrupts
#      every later row's running-balance reconciliation until the next
#      OCR error happens to cancel it back out.
#   2. Inside a DD/MM/YYYY (or DD-MM-YYYY) date, splitting one of the
#      two-digit fields - "03/11/2025" comes back as "03/1 1/2025". That
#      no longer matches any date pattern at all, so the whole
#      transaction row is silently dropped rather than merely misdated.
#
# Both are corrected by literally gluing the split digits back together;
# neither pattern can ever match legitimate prose (a digit directly
# followed by whitespace then a comma-digit group, or a date-shaped
# digit/separator run with a mid-token space, is never how real text or
# a correctly-OCR'd number/date reads).
_OCR_AMOUNT_SPACE_RE = re.compile(r"(?<=\d)[ \t]+(?=,\d{2,3}\b)")
_OCR_DATE_SPACE_RE = re.compile(r"\b(\d)[ \t]?(\d)([-/])(\d)[ \t]?(\d)([-/])(\d{4})\b")


def _normalize_ocr_text(text):
    text = _OCR_AMOUNT_SPACE_RE.sub("", text)
    text = _OCR_DATE_SPACE_RE.sub(r"\1\2\3\4\5\6\7", text)
    return text


def extract_text_with_ocr(path, manual_passwords=None, on_progress=None):
    """
    Extract text from a PDF using OCR (pdf2image + pytesseract).
    Returns (text, num_pages, password_used) to match the signature of the existing read_pdf_text.

    A 50-100 page scanned statement can take tens of minutes to OCR page
    by page, during which this used to report nothing at all - from the
    caller's side (and the UI, once extract_bank.py's progress lines reach
    it) that's indistinguishable from having hung outright. `on_progress`,
    when given, is called as `on_progress(current_page, total_pages)`
    once per page so something keeps moving for as long as OCR is
    actually still running.
    """
    last_error = None
    for password in _password_candidates(path, manual_passwords):
        try:
            extracted_text = []
            num_pages = 0
            total_pages = _page_count(path, password)
            custom_config = r'--oem 3 --psm 6'
            for image in _iter_page_images(path, password):
                num_pages += 1
                if on_progress:
                    on_progress(num_pages, total_pages)
                processed_image = preprocess_image(image)
                text = pytesseract.image_to_string(processed_image, config=custom_config)
                extracted_text.append(text)

            if num_pages == 0:
                raise ValueError("no pages could be rendered from this PDF")

            full_text = _normalize_ocr_text("\n".join(extracted_text))
            return full_text, num_pages, password

        except Exception as exc:
            last_error = exc

    raise last_error


def extract_words_with_boxes(path, manual_passwords=None):
    """
    OCR a PDF and return both the flat text *and* each recognised word's
    position on its page.

    Plain `image_to_string` collapses a table into a single stream of
    text, so a layout that puts (say) "Withdrawals" and "Deposits" in two
    separate columns is indistinguishable once flattened - there's no way
    to tell which column a number came from without re-running OCR in a
    position-aware mode. `image_to_data` gives per-word bounding boxes
    instead, which is what a column-aware parser (e.g. the passbook
    ledger adapter) needs to reconstruct rows correctly.

    Returns (text, num_pages, password_used, pages_words) where
    pages_words is a list (one entry per page) of lists of:
        {"text": str, "left": int, "top": int, "width": int, "height": int,
         "line_num": int, "conf": float}
    in the same left-to-right, top-to-bottom order Tesseract emits them.
    """
    last_error = None
    for password in _password_candidates(path, manual_passwords):
        try:
            extracted_text = []
            pages_words = []
            num_pages = 0
            custom_config = r'--oem 3 --psm 6'

            for image in _iter_page_images(path, password):
                num_pages += 1
                processed_image = preprocess_image(image)
                data = pytesseract.image_to_data(
                    processed_image, config=custom_config, output_type=pytesseract.Output.DICT
                )
                words = []
                for i, word in enumerate(data.get("text", [])):
                    word = word.strip()
                    if not word:
                        continue
                    try:
                        conf = float(data["conf"][i])
                    except (ValueError, TypeError):
                        conf = -1.0
                    words.append({
                        "text": word,
                        "left": int(data["left"][i]),
                        "top": int(data["top"][i]),
                        "width": int(data["width"][i]),
                        "height": int(data["height"][i]),
                        "line_num": int(data["line_num"][i]),
                        "conf": conf,
                    })
                pages_words.append(words)
                extracted_text.append(pytesseract.image_to_string(processed_image, config=custom_config))

            full_text = "\n".join(extracted_text)
            return full_text, num_pages, password, pages_words

        except Exception as exc:
            last_error = exc

    raise last_error
