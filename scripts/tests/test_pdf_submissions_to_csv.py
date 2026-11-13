from pdf_submissions_to_csv import Line, clean, page_to_lines


def raw(lines: list[tuple[str, float]]) -> list[dict]:
    return [{"text": text, "top": top} for text, top in lines]


def test_paragraph_gap_starts_new_paragraph_and_wrapped_lines_join():
    lines = page_to_lines(
        raw(
            [
                ("First paragraph wraps", 100),
                ("onto a second line.", 114),
                ("Second paragraph.", 136),
            ]
        ),
        page=0,
    )
    assert clean([lines]) == "First paragraph wraps onto a second line.\n\nSecond paragraph."


def test_bullets_start_new_paragraphs():
    lines = page_to_lines(
        raw([("Reasons:", 100), ("• one", 114), ("• two which", 128), ("wraps", 142)]), page=0
    )
    assert clean([lines]) == "Reasons:\n\n• one\n\n• two which wraps"


def test_ligatures_and_redaction_gaps_are_normalised():
    lines = page_to_lines(raw([("A ﬁrm     Australian business.", 100)]), page=0)
    assert clean([lines]) == "A firm Australian business."


def test_header_block_and_title_are_dropped():
    lines = page_to_lines(
        raw(
            [
                ("Consultation title", 70),
                ("Submission by:", 100),
                ("Organisation: (ABN: )", 114),
                ("Date: 7 October 2024", 128),
                ("The actual submission.", 150),
            ]
        ),
        page=0,
    )
    assert clean([lines]) == "The actual submission."


def test_trailing_consent_section_is_dropped():
    body = [Line(f"Point {n}.", page=0, new_paragraph=True) for n in range(6)]
    consent = [
        Line("Consent to Publication", page=0, new_paragraph=True),
        Line("I consent to publication.", page=0, new_paragraph=True),
    ]
    assert "Consent" not in clean([body + consent])


def test_page_break_joins_unfinished_sentence_but_not_finished_one():
    page_one = [Line("A sentence that", page=0, new_paragraph=False)]
    page_two = [Line("continues. Done.", page=1, new_paragraph=False)]
    page_three = [Line("Next paragraph.", page=2, new_paragraph=False)]
    assert clean([page_one, page_two, page_three]) == (
        "A sentence that continues. Done.\n\nNext paragraph."
    )


def test_page_numbers_and_repeated_footers_are_dropped():
    pages = [
        [
            Line(f"Body text {n}.", page=n, new_paragraph=True),
            Line("Company confidential", page=n, new_paragraph=False),
            Line(str(n + 1), page=n, new_paragraph=False),
        ]
        for n in range(3)
    ]
    assert clean(pages) == "Body text 0.\n\nBody text 1.\n\nBody text 2."


def test_letterhead_up_to_salutation_is_dropped():
    lines = [
        Line("Level 2, 15 Moore St Canberra", page=0, new_paragraph=True),
        Line("5 November 2024", page=0, new_paragraph=True),
        Line("Dear Sir/Madam,", page=0, new_paragraph=True),
        Line("We recommend X.", page=0, new_paragraph=True),
    ]
    assert clean([lines]) == "We recommend X."


def test_sign_off_and_signatory_are_dropped():
    body = [Line(f"Point {n}.", page=0, new_paragraph=True) for n in range(6)]
    sign_off = [
        Line("Yours sincerely,", page=0, new_paragraph=True),
        Line("Jane Citizen Chief Executive", page=0, new_paragraph=True),
    ]
    assert clean([body + sign_off]).endswith("Point 5.")


def test_table_of_contents_entries_are_dropped():
    lines = [
        Line("Contents", page=0, new_paragraph=True),
        Line("Introduction .................... 3", page=0, new_paragraph=True),
        Line("Real text.", page=0, new_paragraph=True),
    ]
    assert clean([lines]) == "Contents\n\nReal text."


def test_footer_with_changing_page_number_is_dropped():
    pages = [
        [
            Line(f"Body text {n}.", page=n, new_paragraph=True),
            Line(f"Submission title Page {n + 1} of 3", page=n, new_paragraph=False),
        ]
        for n in range(3)
    ]
    assert clean(pages) == "Body text 0.\n\nBody text 1.\n\nBody text 2."


def test_long_tail_after_sign_off_is_kept():
    body = [Line(f"Point {n}.", page=0, new_paragraph=True) for n in range(10)]
    tail = [
        Line("Best regards,", page=0, new_paragraph=True),
        Line("Summary of Recommendations", page=0, new_paragraph=True),
        Line("Recommendation 1: " + "x" * 450, page=0, new_paragraph=True),
    ]
    text = clean([body + tail])
    assert "Best regards" not in text
    assert "Summary of Recommendations" in text
