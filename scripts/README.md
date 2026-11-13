# Consultation setup scripts

Standalone CLI tools for preparing a new consultation for the
ThemeFinder pipeline. They run as a self-contained `uv` project under
`scripts/` — independent of the Django backend env.

| Script | Purpose |
| --- | --- |
| `setup_consultation.py` | Validate a Q.U. workbook against a response file, build the ThemeFinder input layout (`respondents.jsonl`, `question_part_<n>/...`), and upload the result to S3. |
| `build_consult_data_template.py` | Generate `consult_data_template_v003.xlsx` — an opinionated Q.U. workbook skeleton with live in-sheet validation rules and a pre-populated dummy Responses sheet. |
| `build_consult_data_dummy_content.py` | Static word lists used as dummy content by the template builder. Not run directly. |
| `pdf_submissions_to_csv.py` | Turn a folder of PDF submissions (one per respondent) into a one-column CSV for the Consult upload ("Citizen Space" layout), plus a manifest mapping each row to its PDF. Strips submitter details, page furniture and consent sections, and flags scanned PDFs and submissions over the embedding token limit. |

The full set of validation rules implemented in both
`setup_consultation.py` and the in-sheet checks in
`consult_data_template_v003.xlsx` is documented in
[`setup_consultation_checks.md`](./setup_consultation_checks.md).

## Running the scripts

From the repo root:

```bash
# Build the opinionated Q.U. template (writes consult_data_template_v003.xlsx
# next to the script):
make build-consultation-template

# Set up a new consultation end-to-end (validate → build inputs → upload to S3):
make setup-consultation name=my_consultation
```

Stop early with `until=validate` (just print the validation report) or
`until=build` (build inputs locally, skip the S3 upload). Pass
`--dry-run` to list what would be uploaded without contacting S3,
and `--bucket` to point at a non-default bucket.

To run the scripts directly:

```bash
cd scripts
uv run python build_consult_data_template.py
uv run python setup_consultation.py my_consultation --until validate
```

## How `setup_consultation.py` finds files

`setup_consultation.py` works from a per-consultation directory at
`scripts/consultations/<name>/`. The first run creates the directory
and prompts you to drop the response data and Q.U. workbook into it;
subsequent runs auto-discover them. Pass `--responses` / `--qu` to
point at files anywhere on disk.

The S3 upload requires AWS credentials in the environment (e.g. via
`aws-vault exec ...`). Use `until=build` to skip the upload step when
working without credentials.

## Tests

```bash
make test-consultation-scripts
```

Fixtures in `tests/fixtures/setup_consultation/` cover the three
response-file layouts and two Q.U. workbook formats we've seen in real
consultations.

## Converting PDF submissions

When each submission is its own PDF, convert the folder to an upload CSV:

```bash
cd scripts
uv run python pdf_submissions_to_csv.py ~/submissions \
    --output submissions.csv --manifest submissions_manifest.csv \
    --text-dir cleaned_text
```

Upload `submissions.csv` with the "Citizen Space" data source. Keep
`submissions_manifest.csv` out of the upload: it maps CSV row numbers back to
the source PDFs and lists flagged files (`no_text` = scanned, needs OCR;
`over_token_limit` = longer than the 8,192-token embedding limit). Spot-check
`cleaned_text/` first, including that redacted words are really gone: black
boxes drawn over text do not remove it from the PDF.
