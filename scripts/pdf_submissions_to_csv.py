"""Convert a folder of PDF consultation submissions into a Consult upload CSV.

Each PDF becomes one row (one respondent) in a single free-text column, which
matches the "Citizen Space" layout the Consult upload expects: header in row 1,
one respondent per row after that.

For every PDF the script:

  1. Extracts the text layer with pdfplumber. Pages with no usable text layer
     (scanned images, or text drawn as vector outlines) are OCR'd with
     Tesseract when it is installed (``brew install tesseract``), and listed
     in the manifest so they can be proofread.
  2. Removes boilerplate that would otherwise surface as themes: the
     submitter block (``Submission by:``, ``Organisation:``, ``Date:`` ...),
     page numbers, headers/footers repeated across pages, and a trailing
     ``Consent to Publication`` section.
  3. Re-joins lines that the PDF wrapped at page width back into paragraphs,
     using the vertical gap between lines to find paragraph breaks.
  4. Counts tokens and flags submissions over the 8,192-token embedding limit
     (the legacy S3 import cuts those down to their first 1,000 characters).

Two files are written:

  * the upload CSV (``--output``): only the submission text, no filenames;
  * a manifest CSV (``--manifest``): CSV row number -> source filename, page
    count, size and flags. Keep it out of the upload; it is for tracing a
    response back to its PDF and for reviewing flagged files.

Usage (from ``scripts/``)::

    uv run python pdf_submissions_to_csv.py ~/submissions \\
        --output submissions.csv --manifest submissions_manifest.csv \\
        --text-dir cleaned_text

Always spot-check the cleaned text (``--text-dir``) before uploading, and
check that redacted words really are absent from it: a PDF "redacted" with
black boxes drawn over the text still contains the hidden words.
"""

import argparse
import csv
import re
import shutil
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import pdfplumber
import pytesseract
import tiktoken

EMBEDDING_TOKEN_LIMIT = 8192
MIN_TEXT_CHARS = 200

# A page with fewer text-layer characters than this is treated as an image
# (scanned page, or text drawn as vector outlines) and OCR'd instead.
MIN_PAGE_CHARS = 100
OCR_DPI = 300
# OCR'd lines with a mean Tesseract confidence below this are logo/graphic noise.
MIN_OCR_LINE_CONFIDENCE = 60

# Max horizontal gap (points) between characters of one word. pdfplumber's
# default of 3 runs words together in tightly spaced fonts.
X_TOLERANCE = 1.5

# A gap between lines this many times the page's usual line pitch is a
# paragraph break.
PARAGRAPH_GAP_RATIO = 1.3

# Lines at the top and bottom of each page checked for repeated headers/footers.
EDGE_LINES = 3

# Submitter details at the top of a submission. Everything up to and including
# the last of these lines (within the first HEADER_SCAN_LINES lines) is dropped,
# which also removes the consultation title that precedes it.
HEADER_LABEL = re.compile(
    r"^(submission by|submitted by|name|position|role|title|organi[sz]ation|"
    r"company|abn|author|email|phone|tel|telephone|mobile|website|"
    r"(mailing |postal )?address|contact|date)\s*:",
    re.IGNORECASE,
)
HEADER_SCAN_LINES = 20

# Letter-style submissions: everything up to and including the salutation
# (letterhead, date, recipient address) is dropped, if it is near the top.
SALUTATION = re.compile(r"^(dear\b.*|to whom it may concern)[,.:]?$", re.IGNORECASE)

# Trailing sections that are process, not opinion. Cut from the heading to the
# end, but only when the heading is in the last quarter of the submission.
TRAILING_SECTION = re.compile(
    r"^(consent to (publication|publish)|publication consent|"
    r"confidentiality|privacy (statement|notice)|declaration)\b",
    re.IGNORECASE,
)

# Letter sign-offs. The signatory's name and title follow; if only a short
# signature block follows, it is cut too. A longer tail (an appendix or summary
# after the letter) is kept and only the sign-off line itself is dropped.
SIGN_OFF = re.compile(
    r"^(yours (sincerely|faithfully|truly)|sincerely( yours)?|(kind |best |warm )?regards)[,.]?$",
    re.IGNORECASE,
)
MAX_SIGNATURE_BLOCK_CHARS = 400

# Table-of-contents entries: "Introduction ........ 3"
TOC_ENTRY = re.compile(r"(\.\s?){4,}\s*\d+$")

PAGE_NUMBER = re.compile(r"^(page\s+)?\d+(\s*(of|/)\s*\d+)?$", re.IGNORECASE)
TRAILING_PAGE_NUMBER = re.compile(r"(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)
BULLET = re.compile(r"^([•▪◦●○■□➢►\-–*]|\(?\d{1,2}[.)]|\(?[a-z][.)]|[ivx]{1,4}[.)])\s")
SENTENCE_END = re.compile(r"[.!?:][\"'”’)]*$")


@dataclass
class Line:
    text: str
    page: int
    # True when there is a paragraph-sized gap above this line, or it is a bullet.
    new_paragraph: bool


@dataclass
class Submission:
    path: Path
    pages: int = 0
    text: str = ""
    tokens: int = 0
    flags: list[str] = field(default_factory=list)


def normalise(text: str) -> str:
    """NFKC fixes ligatures (ﬁ -> fi); redaction gaps leave runs of spaces."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def page_to_lines(raw_lines: list[dict], page: int) -> list[Line]:
    """Turn pdfplumber text lines (``text``, ``top``) into Lines with breaks."""
    raw_lines = [line for line in raw_lines if normalise(line["text"])]
    tops = [line["top"] for line in raw_lines]
    pitches = [b - a for a, b in pairwise(tops) if b > a]
    # Usual line pitch = most common gap (smallest on ties). A median would be
    # skewed upwards on short pages where paragraph gaps are half the gaps.
    counts = Counter(round(p) for p in pitches)
    pitch = min(p for p, c in counts.items() if c == max(counts.values())) if counts else 0

    lines = []
    for index, raw in enumerate(raw_lines):
        text = normalise(raw["text"])
        gap = tops[index] - tops[index - 1] if index else 0
        big_gap = bool(pitch) and gap > pitch * PARAGRAPH_GAP_RATIO
        lines.append(Line(text, page, bool(BULLET.match(text)) or big_gap))
    return lines


def drop_repeated_edges(pages: list[list[Line]]) -> list[list[Line]]:
    """Drop page numbers and header/footer lines repeated on most pages."""

    def key(text: str) -> str:
        # Mask a trailing page number so "Title Page 3 of 9" repeats across pages.
        return TRAILING_PAGE_NUMBER.sub("#", text)

    edge_counts: Counter[str] = Counter()
    for lines in pages:
        edge_counts.update({key(line.text) for line in lines[:EDGE_LINES] + lines[-EDGE_LINES:]})

    repeated = (
        {text for text, count in edge_counts.items() if count >= len(pages) / 2}
        if len(pages) >= 3
        else set()
    )
    return [
        [
            line
            for line in lines
            if key(line.text) not in repeated
            and not PAGE_NUMBER.match(line.text)
            and not TOC_ENTRY.search(line.text)
        ]
        for lines in pages
    ]


def drop_header_block(lines: list[Line]) -> list[Line]:
    last_label = None
    for index, line in enumerate(lines[:HEADER_SCAN_LINES]):
        if HEADER_LABEL.match(line.text):
            last_label = index
        if SALUTATION.match(line.text):
            last_label = index
    return lines if last_label is None else lines[last_label + 1 :]


def drop_trailing_section(lines: list[Line]) -> list[Line]:
    for index, line in enumerate(lines):
        if index < len(lines) * 0.75:
            continue
        if TRAILING_SECTION.match(line.text):
            return lines[:index]
        if SIGN_OFF.match(line.text):
            tail = lines[index + 1 :]
            if sum(len(rest.text) for rest in tail) <= MAX_SIGNATURE_BLOCK_CHARS:
                return lines[:index]
            return lines[:index] + tail
    return lines


def join_paragraphs(lines: list[Line]) -> str:
    paragraphs: list[str] = []
    for index, line in enumerate(lines):
        # Across a page break, start a new paragraph only if the previous
        # page finished its sentence.
        page_break = (
            index > 0 and line.page != lines[index - 1].page and SENTENCE_END.search(paragraphs[-1])
        )
        if not paragraphs or line.new_paragraph or page_break:
            paragraphs.append(line.text)
        else:
            paragraphs[-1] = f"{paragraphs[-1]} {line.text}"
    return "\n\n".join(paragraphs)


def clean(pages: list[list[Line]]) -> str:
    lines = [line for page in drop_repeated_edges(pages) for line in page]
    return join_paragraphs(drop_trailing_section(drop_header_block(lines)))


def ocr_page(page: pdfplumber.page.Page) -> list[dict]:
    """OCR a page image into pdfplumber-style lines (``text``, ``top`` in points)."""
    image = page.to_image(resolution=OCR_DPI).original
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    scale = OCR_DPI / 72

    words: dict[tuple[int, int, int], list[tuple[int, int, str, float]]] = {}
    for i, word in enumerate(data["text"]):
        if word.strip():
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            words.setdefault(key, []).append(
                (data["left"][i], data["top"][i], word, float(data["conf"][i]))
            )

    lines = [
        {
            "text": " ".join(word for _, _, word, _ in sorted(line)),
            "top": min(top for _, top, _, _ in line) / scale,
        }
        for line in words.values()
        if sum(conf for *_, conf in line) / len(line) >= MIN_OCR_LINE_CONFIDENCE
    ]
    return sorted(lines, key=lambda line: line["top"])


def page_ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5] -> '1-3, 5'"""
    ranges: list[list[int]] = []
    for number in numbers:
        if ranges and number == ranges[-1][-1] + 1:
            ranges[-1].append(number)
        else:
            ranges.append([number])
    return ", ".join(f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]) for r in ranges)


def convert(path: Path, encoding: tiktoken.Encoding, ocr: bool) -> Submission:
    submission = Submission(path=path)
    ocr_pages: list[int] = []
    image_pages: list[int] = []
    try:
        pages = []
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages):
                if len(page.chars) >= MIN_PAGE_CHARS:
                    raw_lines = page.extract_text_lines(x_tolerance=X_TOLERANCE)
                elif ocr:
                    raw_lines = ocr_page(page)
                    ocr_pages.append(number + 1)
                else:
                    raw_lines = page.extract_text_lines(x_tolerance=X_TOLERANCE)
                    image_pages.append(number + 1)
                pages.append(page_to_lines(raw_lines, number))
    except Exception as error:  # noqa: BLE001 - report and carry on with the batch
        submission.flags.append(f"unreadable: {error}")
        return submission

    submission.pages = len(pages)
    submission.text = clean(pages)
    submission.tokens = len(encoding.encode(submission.text))

    if ocr_pages:
        submission.flags.append(f"ocr: pages {page_ranges(ocr_pages)}")
    if image_pages:
        submission.flags.append(
            f"image_pages: {page_ranges(image_pages)} have no text layer, "
            "install tesseract to OCR them"
        )
    if len(submission.text) < MIN_TEXT_CHARS:
        submission.flags.append("no_text: nothing extracted")
    if submission.tokens > EMBEDDING_TOKEN_LIMIT:
        submission.flags.append(f"over_token_limit: {submission.tokens} > {EMBEDDING_TOKEN_LIMIT}")
    return submission


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "input_dir", type=Path, help="Folder of submission PDFs (searched recursively)"
    )
    parser.add_argument("--output", type=Path, default=Path("submissions.csv"))
    parser.add_argument("--manifest", type=Path, default=Path("submissions_manifest.csv"))
    parser.add_argument("--column", default="Submission", help="Header for the free-text column")
    parser.add_argument("--text-dir", type=Path, help="Also write each cleaned submission as .txt")
    parser.add_argument(
        "--no-ocr", action="store_true", help="Don't OCR pages that have no text layer"
    )
    args = parser.parse_args(argv)

    ocr = not args.no_ocr and shutil.which("tesseract") is not None
    if not args.no_ocr and not ocr:
        print("tesseract not found: image-only pages will be flagged, not OCR'd", file=sys.stderr)

    pdfs = sorted(p for p in args.input_dir.rglob("*") if p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"No PDFs found in {args.input_dir}", file=sys.stderr)
        return 1

    encoding = tiktoken.get_encoding("cl100k_base")
    if args.text_dir:
        args.text_dir.mkdir(parents=True, exist_ok=True)

    with (
        args.output.open("w", newline="", encoding="utf-8") as output_file,
        args.manifest.open("w", newline="", encoding="utf-8") as manifest_file,
    ):
        output = csv.writer(output_file)
        manifest = csv.writer(manifest_file)
        output.writerow([args.column])
        manifest.writerow(["csv_row", "file", "pages", "chars", "tokens", "flags"])

        csv_row = 1  # the header row
        for path in pdfs:
            submission = convert(path, encoding, ocr)
            included = bool(submission.text) and not any(
                flag.startswith(("unreadable", "no_text")) for flag in submission.flags
            )
            if included:
                csv_row += 1
                output.writerow([submission.text])
            if args.text_dir and submission.text:
                (args.text_dir / f"{path.stem}.txt").write_text(submission.text, encoding="utf-8")
            manifest.writerow(
                [
                    csv_row if included else "",
                    str(path.relative_to(args.input_dir)),
                    submission.pages,
                    len(submission.text),
                    submission.tokens,
                    "; ".join(submission.flags),
                ]
            )
            status = "ok" if not submission.flags else "; ".join(submission.flags)
            print(f"{'included' if included else 'SKIPPED '}  {path.name}: {status}")

    written = csv_row - 1
    print(f"\nWrote {written} of {len(pdfs)} submissions to {args.output}")
    print(f"Manifest (row -> file, flags): {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
