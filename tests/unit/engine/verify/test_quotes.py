"""``verify/quotes.py``: normalization, quote location, and the text-claim checks (M06 3.3, 4.1;
ADR-049 D4, D7 and its spec decisions, later rounds winning over earlier ones and all of them
over the stub's docstring). The verifier has no entailment stage. Written before the
implementation."""

from __future__ import annotations

import pytest

from app.engine.verify import numbers, quotes
from app.engine.verify.models import CheckOutcome, VerifyConfig, allowed_ciks
from app.engine.verify.quotes import check_text, locate_quote, normalize
from eval.verifier_inputs import evidence_for
from tests.unit.engine.verify import verify_cases as vc

pytestmark = pytest.mark.xfail(
    condition=not (quotes.IMPLEMENTED and numbers.IMPLEMENTED),
    raises=NotImplementedError,
    strict=True,
    reason="Shekar-owned verify/quotes.py and verify/numbers.py are not implemented yet",
)

CFG = VerifyConfig()
DECIDED = ("quote_substring", "metadata_match", "number_in_text")
SALES_QUOTE = "Net sales decreased 3% during 2023 compared to 2022."
APPLE_ONLY = frozenset({vc.APPLE})


def run(case: vc.Case, cfg: VerifyConfig = CFG) -> CheckOutcome:
    return check_text(case.claim, case.evidence, case.ctx, cfg)


def assert_passes(outcome: CheckOutcome) -> None:
    got = {name: getattr(outcome.checks, name) for name in DECIDED}
    assert got == dict.fromkeys(DECIDED, "pass"), outcome.reason
    assert outcome.checks.provenance_exec == "n/a"
    assert outcome.checks.entailment == "n/a"
    assert outcome.passed and outcome.reason == ""


def assert_fails(outcome: CheckOutcome, check: str, *, others_pass: tuple[str, ...] = ()) -> None:
    assert getattr(outcome.checks, check) == "fail", outcome
    for name in others_pass:
        assert getattr(outcome.checks, name) == "pass", (name, outcome)
    assert outcome.checks.provenance_exec == "n/a"
    assert outcome.checks.entailment == "n/a"
    assert not outcome.passed and outcome.reason.strip()


# --- normalize ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Net SALES", "net sales"),
        ("  net \t sales\r\n\n rose  ", "net sales rose"),
        ("net\u00a0sales\u202frose", "net sales rose"),
        ("net sa\u200bles", "net sales"),
        ("net \u200b sales", "net sales"),
        ("\u2018a\u2019 \u201cb\u201d", "'a' \"b\""),
        ("a\u2010b\u2011c\u2012d\u2013e\u2014f\u2015g\u2212h", "a-b-c-d-e-f-g-h"),
        ("STRA\u00dfE", "strasse"),
        (" \u00a0\u200b ", ""),
    ],
)
def test_normalize_folds_only_presentation_differences(raw: str, expected: str) -> None:
    """Case, whitespace (with non-breaking spaces), zero-width spaces, curly quotes and dash
    variants are presentation. Removing a zero-width space between two spaces must still leave
    one space."""
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("marked", "plain"),
    [("revenue\u00b2", "revenue2"), ("1\u00bd", "11\u20442"), ("\uff15%", "5%")],
)
def test_normalize_is_not_nfkc(marked: str, plain: str) -> None:
    """NFKC would turn a footnote mark into a digit and let it support a number, so the
    character map must leave compatibility characters alone. Presentation still folds, so a
    function that changes nothing does not pass."""
    assert normalize(marked) != normalize(plain)
    assert normalize(f"  {plain.upper()} ") == normalize(plain)


# --- locate_quote -------------------------------------------------------------------------------


def located(quote: str, text: str) -> str:
    span = locate_quote(quote, text)
    assert span is not None, (quote, text)
    return text[span[0] : span[1]]


def test_locate_returns_offsets_into_the_original_text() -> None:
    """The span indexes the original passage, not its normalized form, so the UI can highlight
    it, and it is the smallest one: no surrounding space (decision O)."""
    text = "Intro.\n\n  NET\u00a0\u00a0Sales   rose \u2014 sharply. End."
    assert located("  net sales rose - sharply ", text) == (
        "NET\u00a0\u00a0Sales   rose \u2014 sharply"
    )


def test_locate_offsets_survive_case_folding_that_changes_length() -> None:
    """Case folding lengthens some characters (sharp s, ligatures); offsets taken from the
    folded string would drift."""
    text = "Die Stra\u00dfe und das \ufb01le: net sales rose sharply here."
    assert located("net sales rose sharply", text) == "net sales rose sharply"


def test_locate_returns_the_first_occurrence() -> None:
    """With two matches the first one is returned."""
    text = "Sales rose fast. Later, sales rose fast again."
    span = locate_quote("sales rose fast", text)
    assert span is not None and span[0] < 5
    assert text[span[0] : span[1]] == "Sales rose fast"


@pytest.mark.parametrize("quote", ["sales fell fast", "", "   ", "\u200b"])
def test_locate_returns_none_for_absent_or_empty_quotes(quote: str) -> None:
    """No match, or a quote that normalizes to nothing, has no location."""
    assert locate_quote(quote, "Sales rose fast.") is None


# --- quote_substring ----------------------------------------------------------------------------


def test_exact_quote_is_kept() -> None:
    """A correct claim with an exact quote passes all three checks."""
    assert_passes(run(vc.text_case("Net sales decreased 3% during 2023.", SALES_QUOTE)))


def test_quote_differing_only_in_presentation_is_kept() -> None:
    """Quote and passage are normalized the same way, whichever side carries the curly quote,
    odd space or capital (M06 pitfall: asymmetry)."""
    passage = "The company\u2019s net\u00a0sales fell \u2014 mainly in\nEurope."
    case = vc.text_case(
        "The company's net sales fell mainly in Europe.",
        "THE COMPANY'S NET SALES FELL - mainly in Europe",
        passage=passage,
    )
    assert_passes(run(case))


def test_presentation_differences_on_the_quote_side_are_kept() -> None:
    """The mirror case: a decorated quote over a plain passage."""
    case = vc.text_case(
        "Net sales fell in Europe.",
        "net\u00a0sales\u200b  fell\nin \u201cEurope\u201d",
        passage='Overall net sales fell in "Europe" last year.',
    )
    assert_passes(run(case))


@pytest.mark.parametrize(
    "quote",
    [
        "Net sales decreased 6% during 2023 compared to 2022.",  # a changed figure
        "Net sales fell by 3% in 2023 versus 2022.",  # a paraphrase
        "Net sales decreased 3% during 2023 driven by foreign currency headwinds.",  # stitched
        "The company expects further declines next year.",  # invented
    ],
)
def test_quote_absent_from_the_chunk_fails(quote: str) -> None:
    """A quote that is not one contiguous span of the passage fails, however plausible."""
    assert_fails(run(vc.text_case("Net sales fell.", quote)), "quote_substring")


@pytest.mark.parametrize("quote", ["", "   ", "\u200b\u00a0"])
def test_empty_quote_fails(quote: str) -> None:
    """An empty quote is a substring of everything, so it must fail explicitly (a required
    false-keep case)."""
    assert_fails(run(vc.text_case("Net sales fell.", quote)), "quote_substring")


@pytest.mark.parametrize(
    "quote",
    ["Net sales", "sales.", "Products | $ | 298,085", "| $ |"],
)
def test_quote_shorter_than_three_words_fails(quote: str) -> None:
    """D4: fewer than three tokens with a letter or digit is no evidence. Table pipes and
    currency signs are not words."""
    assert_fails(run(vc.text_case("Net sales fell.", quote)), "quote_substring")


@pytest.mark.parametrize("quote", ["Net sales decreased", "Products | $ | 298,085 | 316,199"])
def test_quote_of_three_words_is_enough(quote: str) -> None:
    """The boundary: three words, digits counting as words."""
    assert run(vc.text_case("Net sales fell.", quote)).checks.quote_substring == "pass"


def test_minimum_quote_length_comes_from_the_config() -> None:
    """``cfg.min_quote_words`` sets the minimum."""
    case = vc.text_case("Net sales fell.", "Net sales")
    assert run(case, VerifyConfig(min_quote_words=2)).checks.quote_substring == "pass"
    assert run(case, VerifyConfig(min_quote_words=3)).checks.quote_substring == "fail"


def test_footnote_superscript_does_not_satisfy_a_digit_quote() -> None:
    """A passage with a footnote mark does not contain the digit."""
    case = vc.text_case(
        "Net sales were 2.", "net sales were 2", passage="Our net sales were \u00b2 restated."
    )
    assert_fails(run(case), "quote_substring")


def test_text_claim_citing_facts_fails() -> None:
    """A text claim's provenance must be a chunk."""
    case = vc.revenue_case()
    claim = vc.edit(case.claim, kind="text")
    assert_fails(run(case._replace(claim=claim)), "quote_substring")


def test_citation_of_a_chunk_not_in_evidence_fails() -> None:
    """A chunk id that is not in the evidence does not exist for the check."""
    case = vc.text_case("Net sales decreased 3%.", SALES_QUOTE)
    claim = vc.text_claim("Net sales decreased 3%.", SALES_QUOTE, "chunk-does-not-exist")
    assert_fails(run(case._replace(claim=claim)), "quote_substring")


def test_real_chunk_the_composer_was_not_shown_fails() -> None:
    """The chunk exists in the corpus and holds the quote, but the composer never saw it: the
    evidence omits it and the claim fails (a required false-keep case)."""
    case = vc.dev_case("fb-00494", "c2")
    row = vc.dev_row("fb-00494")
    cited = case.claim.provenance.model_dump()["chunk_id"]
    assert cited in vc.dev_trace().chunks and cited in row.shown_chunk_ids
    hidden = row.model_copy(
        update={"shown_chunk_ids": [i for i in row.shown_chunk_ids if i != cited]}
    )
    outcome = run(case._replace(evidence=evidence_for(vc.dev_trace(), hidden)))
    assert_fails(outcome, "quote_substring")


# --- metadata_match: the chunk's company --------------------------------------------------------


def test_chunk_of_another_company_fails_metadata() -> None:
    """A real quote from the wrong company's filing is not evidence about this company."""
    case = vc.text_case("Net sales decreased 3%.", SALES_QUOTE, cik=vc.MICROSOFT)
    outcome = run(case)
    assert_fails(outcome, "metadata_match", others_pass=("quote_substring", "number_in_text"))


def test_chunk_without_a_company_fails_when_companies_are_restricted() -> None:
    """A NULL cik cannot be shown to be an allowed company (D7: fail closed)."""
    outcome = run(vc.text_case("Net sales decreased 3%.", SALES_QUOTE, cik=None))
    assert_fails(outcome, "metadata_match", others_pass=("quote_substring",))


@pytest.mark.parametrize("cik", [vc.APPLE, vc.MICROSOFT, None])
def test_narrative_plan_with_no_company_is_unrestricted(cik: int | None) -> None:
    """With no company named, ``allowed_ciks`` is empty and means no restriction, for any cik
    including NULL (a required false-drop case)."""
    query = vc.plan(intent="narrative", ciks=[], metrics=[], operation=None)
    assert allowed_ciks(query) == frozenset()
    case = vc.text_case("Net sales decreased 3%.", SALES_QUOTE, cik=cik, query=query)
    assert_passes(run(case))


def test_any_allowed_company_may_be_cited() -> None:
    """With two companies allowed, either one's passage is evidence."""
    case = vc.text_case(
        "Net sales decreased 3%.",
        SALES_QUOTE,
        cik=vc.MICROSOFT,
        allowed=frozenset({vc.APPLE, vc.MICROSOFT}),
    )
    assert_passes(run(case))


# --- number_in_text: the number-in-quote rule ---------------------------------------------------


@pytest.mark.parametrize(
    "claim_text",
    [
        "Net sales decreased 8% during 2023.",  # the source says 3%
        "Net sales decreased 30% during 2023.",
        "Net sales decreased 3.4% during 2023.",
        "Net sales decreased 3% to $383.3 billion.",  # a second, unquoted figure
    ],
)
def test_number_changed_in_claim_text_fails(claim_text: str) -> None:
    """Every value in the claim must be stated by the quote."""
    outcome = run(vc.text_case(claim_text, SALES_QUOTE))
    assert_fails(outcome, "number_in_text", others_pass=("quote_substring", "metadata_match"))


def test_number_elsewhere_in_the_chunk_does_not_count() -> None:
    """44.1% is in the passage but not in the quote: only the quote supports a number."""
    case = vc.text_case("Gross margin was 44.1%.", SALES_QUOTE)
    assert_fails(run(case), "number_in_text")


@pytest.mark.parametrize(
    ("claim_text", "expected"),
    [
        ("Product sales were $55,893 million.", "pass"),
        ("Product sales were $55.9 billion.", "pass"),  # a rounding at its own precision
        ("Product sales were $56 billion.", "pass"),
        ("Product sales were $56.0 billion.", "fail"),  # close but wrong
        ("Product sales were $55.8 billion.", "fail"),
        ("Product sales were $55,894 million.", "fail"),
        ("Product sales were $55,893 billion.", "fail"),  # million vs billion
    ],
)
def test_a_quoted_figure_is_compared_by_value(claim_text: str, expected: str) -> None:
    """Number matching compares parsed values, with unit and scale applied, within the claim
    number's own precision."""
    case = vc.text_case(
        claim_text,
        "sales of products were $55,893 million",
        passage="In total, sales of products were $55,893 million last year.",
    )
    assert run(case).checks.number_in_text == expected


@pytest.mark.parametrize(
    ("claim_text", "quote"),
    [
        ("Operating margin was 5.5%.", "operating margin was 55% this year"),
        ("Operating margin was 55%.", "operating margin was 5.5% this year"),
        ("The company closed 3% of sites.", "the company closed 3 stores this year"),
        ("Net sales decreased 3 during 2023.", "Net sales decreased 3% during 2023"),
        ("Net sales were $298 million.", "net sales were $298,085 million overall"),
        ("The company opened 5 stores.", "the company opened 55 stores this year"),
    ],
    ids=["5.5%!=55%", "55%!=5.5%", "count!=percent", "percent!=count",
         "digit_prefix", "digit_substring"],
)  # fmt: skip
def test_equal_digits_with_another_value_do_not_match(claim_text: str, quote: str) -> None:
    """Never a raw digit substring: 0.055 is not 0.55, and 3 is not 3%."""
    case = vc.text_case(claim_text, quote, passage=f"Overview: {quote}.")
    assert_fails(run(case), "number_in_text", others_pass=("quote_substring",))


def test_claim_cannot_be_more_precise_than_its_quote() -> None:
    """ "$55,893 million" is not established by a quote that says "$55.9 billion"."""
    case = vc.text_case(
        "Sales were $55,893 million.",
        "sales were $55.9 billion",
        passage="Our sales were $55.9 billion.",
    )
    assert_fails(run(case), "number_in_text")


def test_a_bare_table_number_with_no_declared_scale_cannot_support_a_scaled_claim() -> None:
    """Fail closed: nothing in the passage says 32,502 is in millions."""
    case = vc.text_case(
        "Total net sales were $32.5 billion.",
        "Total net sales 32,502 31,000",
        passage="Total net sales 32,502 31,000",
    )
    assert_fails(run(case), "number_in_text", others_pass=("quote_substring",))


def test_a_bare_table_number_takes_its_declared_scale_then_compares_by_value() -> None:
    """32,502 in millions is 32.502 billion, which "$32.5 billion" states at its precision."""
    case = vc.text_case(
        "Total net sales were $32.5 billion.",
        "Total net sales | 32,502 | 31,000",
        passage="(in millions) Segment results. Total net sales | 32,502 | 31,000",
    )
    assert_passes(run(case))


@pytest.mark.parametrize("scaled", ["6,098 million", "6,098 billion", "$6,098 million"])
def test_unscaled_source_supports_no_scale_word(scaled: str) -> None:
    """With no scale declared in the passage, a bare number supports neither million nor
    billion: fail closed."""
    case = vc.text_case(
        f"Total inventories were {scaled}.",
        "Total inventories 6,098 5,500",
        passage="Balance sheet data. Total inventories 6,098 5,500",
    )
    assert_fails(run(case), "number_in_text")


def test_bare_claim_number_over_a_bare_table_number_is_kept() -> None:
    """The control for D4: without a scale word the digits do state the number."""
    case = vc.text_case(
        "Total inventories were 6,098.",
        "Total inventories 6,098 5,500",
        passage="Balance sheet data. Total inventories 6,098 5,500",
    )
    assert_passes(run(case))


@pytest.mark.parametrize(
    "claim_text",
    [
        "In fiscal 2023, net sales decreased.",
        "In Q4 2023, net sales decreased.",
        "As of December 31, 2023, net sales had decreased.",
        "Per Item 7 of the 10-K, net sales decreased [1].",
    ],
)
def test_years_and_labels_need_no_support_in_the_quote(claim_text: str) -> None:
    """Years, quarters, days, item and form names assert no quantity, so a quote without them
    still supports the claim."""
    case = vc.text_case(claim_text, "The decrease was primarily driven by foreign currency")
    assert_passes(run(case))


def test_asserted_zero_missing_from_the_quote_fails() -> None:
    """A number the claim gives as its answer is not exempt, however small."""
    case = vc.text_case(
        "Restructuring costs on the income statement are 0.",
        "Consolidated Statement of Income",
        passage="Consolidated Statement of Income (in millions) Net revenue 86,392",
    )
    outcome = run(case)
    assert_fails(outcome, "number_in_text", others_pass=("quote_substring", "metadata_match"))


# --- documented limits: no entailment stage -----------------------------------------------------


def test_added_cause_over_a_real_quote_passes_documented_limit() -> None:
    """DOCUMENTS A LIMIT, not desired behavior: the quote is real and the numbers agree, but the
    cause is invented. Without an entailment stage the claim passes (ADR-049)."""
    case = vc.text_case(
        "Net sales decreased 3% during 2023 because demand in China collapsed.", SALES_QUOTE
    )
    assert_passes(run(case))


def test_partially_supported_claim_passes_documented_limit() -> None:
    """DOCUMENTS A LIMIT: half of the claim (the outlook) is in no source, and the
    deterministic checks cannot see that."""
    case = vc.text_case(
        "Net sales decreased 3% during 2023 and management expects the decline to continue.",
        SALES_QUOTE,
    )
    assert_passes(run(case))


def test_wrong_year_in_a_text_claim_passes_documented_limit() -> None:
    """DOCUMENTS A LIMIT that follows from the year exemption: a text claim's year is not
    compared with its quote."""
    case = vc.text_case("Net sales decreased 3% during 2019.", SALES_QUOTE)
    assert_passes(run(case))


# --- real dev claims from the Gate 0 trace ------------------------------------------------------


@pytest.mark.parametrize(
    ("question_id", "claim_id"),
    [
        ("fb-00494", "c2"),  # "787" product name and the year
        ("fb-00746", "c1"),  # "12(b)"
        ("fb-00651", "c2"),  # quarter token and year
        ("fb-00724", "c1"),  # quarter token and year
        ("fb-03282", "c1"),  # day of month and year
        ("fb-02419", "c1"),  # names Viatris, another tracked company, in Pfizer's passage
    ],
)
def test_dev_claim_missing_only_exempt_tokens_is_kept(question_id: str, claim_id: str) -> None:
    """Real dev claims whose quotes omit only exempt tokens must be kept; dropping them would
    be a measured false drop. ``fb-00746 c1`` ("NASDAQ") and ``fb-02419 c1`` ("Viatris") name
    tracked companies in text claims, where the company-name rule does not apply."""
    assert_passes(run(vc.dev_case(question_id, claim_id)))


def test_dev_claim_whose_asserted_zero_is_unquoted_is_dropped() -> None:
    """fb-01328 c1 quotes only the statement's title: the quote is real, but its answer "0" is
    not in it."""
    outcome = run(vc.dev_case("fb-01328", "c1"))
    assert_fails(outcome, "number_in_text", others_pass=("quote_substring", "metadata_match"))


@pytest.mark.parametrize(
    ("question_id", "claim_id"),
    [("fb-00651", "c1"), ("fb-00723", "c1")],
)
def test_dev_semantic_over_claim_passes_documented_limit(question_id: str, claim_id: str) -> None:
    """DOCUMENTS A LIMIT: the uncalibrated judge marked these claims partial (semantic
    over-claiming). Their quotes and numbers check out, so the deterministic verifier keeps
    them (ADR-049, recorded consequences)."""
    assert_passes(run(vc.dev_case(question_id, claim_id)))


# --- B: a quote may not cut through a number -----------------------------------------------------

CUT_PASSAGE = "Total net sales were $298,085 million in 2023. Margins grew 16% during 2023."


@pytest.mark.parametrize(
    ("claim_text", "quote"),
    [
        ("Net sales were $298.", "net sales were $298"),  # from $298,085
        ("Margins grew 6% during 2023.", "6% during 2023"),  # from 16%
        ("Net sales were 85 million.", "085 million in 2023"),  # starts inside 298,085
    ],
    ids=["ends_inside_a_grouped_number", "starts_inside_a_percentage", "starts_inside_group"],
)
def test_a_quote_that_cuts_a_number_fails(claim_text: str, quote: str) -> None:
    """Decision B: a quote span may not begin or end inside a numeric token."""
    assert quote in CUT_PASSAGE  # a plain substring: only the token boundary can reject it
    case = vc.text_case(claim_text, quote, passage=CUT_PASSAGE)
    assert_fails(run(case), "quote_substring")


def test_a_quote_may_not_stop_before_a_decimal_part() -> None:
    case = vc.text_case(
        "Gross margin was 44.", "gross margin was 44", passage="Our gross margin was 44.1%."
    )
    assert_fails(run(case), "quote_substring")


@pytest.mark.parametrize(
    ("quote", "passage"),
    [
        ("net sales were 298", "In total net sales were 298."),  # a sentence's full stop
        ("net sales were 298", "In total net sales were 298, up from 250."),
        ("net sales were $298,085 million", "Total net sales were $298,085 million in 2023."),
    ],
)
def test_punctuation_after_a_whole_number_is_not_part_of_it(quote: str, passage: str) -> None:
    case = vc.text_case("Net sales were reported.", quote, passage=passage)
    assert run(case).checks.quote_substring == "pass"


def test_a_later_whole_occurrence_is_still_found() -> None:
    """The first match sits inside "16%"; the second one is a real occurrence."""
    passage = "Up 16% during 2023 abroad, and up only 6% during 2023 at home."
    case = vc.text_case("Sales rose 6% at home.", "6% during 2023", passage=passage)
    assert run(case).checks.quote_substring == "pass"
    span = locate_quote("6% during 2023", passage)
    assert span is not None and span[0] == passage.index("only ") + len("only ")


def test_locate_does_not_find_a_cut_number() -> None:
    assert locate_quote("net sales were $298", CUT_PASSAGE) is None


# --- scale: only one declared in the same passage -------------------------------------------------


@pytest.mark.parametrize(
    ("question_id", "claim_id"),
    [
        ("fb-00438", "c2"),  # "(In millions, except per share data)"
        ("fb-00517", "c1"),  # "(Dollars in millions, ...)"
        ("fb-00517", "c2"),
        ("fb-00517", "c3"),
        ("fb-00711", "c1"),  # "in Millions Except Per Share"
        ("fb-04171", "c1"),  # "(In thousands, ...)" with non-breaking spaces
    ],
)
def test_dev_claim_scaled_by_its_passages_declaration_is_kept(
    question_id: str, claim_id: str
) -> None:
    """The six real dev claims that write a scale word over a bare table cell: the cell takes
    the passage's declared scale and is then compared by value."""
    assert_passes(run(vc.dev_case(question_id, claim_id)))


MILLIONS_TABLE = "Statement of Earnings (in millions) Net sales | 32,502 | 31,000"
TABLE_QUOTE = "Net sales | 32,502 | 31,000"


@pytest.mark.parametrize(
    "header",
    [
        "(in millions)",
        "(Dollars in millions, except per share data)",
        "(Unaudited; in Millions Except Per Share Figures)",
        "(In\u00a0millions)",
    ],
)
def test_a_declared_scale_supports_the_same_scale_word(header: str) -> None:
    passage = f"Statement of Earnings {header} {TABLE_QUOTE}"
    case = vc.text_case("Net sales were $32,502 million.", TABLE_QUOTE, passage=passage)
    assert_passes(run(case))


@pytest.mark.parametrize("scale", ["billion", "thousand", "trillion"])
def test_a_scale_the_passage_does_not_declare_fails(scale: str) -> None:
    """Required: a "billion" claim over a cell declared "in millions" is dropped."""
    case = vc.text_case(f"Net sales were $32,502 {scale}.", TABLE_QUOTE, passage=MILLIONS_TABLE)
    assert_fails(run(case), "number_in_text", others_pass=("quote_substring", "metadata_match"))


def test_a_header_inside_the_quote_declares_the_scale_too() -> None:
    quote = "(in millions) Net sales | 32,502"
    passage = f"Segment results {quote} | 31,000"
    assert_passes(run(vc.text_case("Net sales were $32,502 million.", quote, passage=passage)))
    assert_passes(run(vc.text_case("Net sales were $32.5 billion.", quote, passage=passage)))
    wrong = vc.text_case("Net sales were $32.5 million.", quote, passage=passage)
    assert_fails(run(wrong), "number_in_text")


@pytest.mark.parametrize("written", ["$32,502", "32,502"])
def test_a_claim_without_a_scale_word_cannot_state_a_scaled_cell(written: str) -> None:
    """The cell is 32,502 million, so the bare figure is wrong by a factor of a million, with or
    without a currency sign."""
    case = vc.text_case(f"Net sales were {written}.", TABLE_QUOTE, passage=MILLIONS_TABLE)
    assert_fails(run(case), "number_in_text")


def test_a_scale_declared_in_another_passage_does_not_count() -> None:
    case = vc.text_case(
        "Net sales were $32,502 million.", TABLE_QUOTE, passage=f"Results. {TABLE_QUOTE}"
    )
    other = vc.chunk("Statement of Earnings (in millions)", chunk_id="chunk-2")
    case = case._replace(evidence=vc.evidence(chunks=[*case.evidence.chunks.values(), other]))
    assert_fails(run(case), "number_in_text", others_pass=("quote_substring",))


def test_a_scale_in_running_prose_does_not_declare_the_tables_scale() -> None:
    """ "a $5 billion buyback" says nothing about a bare cell elsewhere."""
    passage = "We announced a $5 billion buyback. Total inventories 6,098 5,500"
    case = vc.text_case(
        "Total inventories were 6,098 billion.", "Total inventories 6,098 5,500", passage=passage
    )
    assert_fails(run(case), "number_in_text")


def test_a_numbers_own_scale_wins_over_the_declared_one() -> None:
    """ "$5.5 billion" is not a bare number, so the header's "in millions" does not apply to
    it: "$5.5 million" is wrong."""
    quote = "a buyback of $5.5 billion was approved"
    passage = f"(Dollars in millions) Overview: {quote}."
    right = vc.text_case("The buyback was $5.5 billion.", quote, passage=passage)
    assert_passes(run(right))
    wrong = vc.text_case("The buyback was $5.5 million.", quote, passage=passage)
    assert_fails(run(wrong), "number_in_text")


def test_a_per_share_cell_is_outside_an_except_per_share_declaration() -> None:
    """The dev passages declare "in millions, except per share data": the declaration itself
    leaves per-share cells unscaled."""
    quote = "Diluted earnings per share | 6.13 | 5.89"
    passage = f"(In millions, except per share data) {quote}"
    case = vc.text_case("Diluted earnings per share were $6.13 per share.", quote, passage=passage)
    assert_passes(run(case))


# --- the numeric token includes its leading sign or parenthesis ----------------------------------


@pytest.mark.parametrize(
    ("passage", "quote"),
    [
        ("Net sales changed -3% this year.", "3% this year"),
        ("Net sales changed \u22123% this year.", "3% this year"),
        ("Net sales change (6)% this year versus last.", "6)% this year"),
        ("The segment recorded -$3 million in the period.", "$3 million in the period"),
    ],
    ids=["minus", "unicode_minus", "parenthesis", "minus_before_currency"],
)
def test_a_quote_may_not_cut_off_a_leading_sign(passage: str, quote: str) -> None:
    """ "3%" taken from "-3%" would state the opposite number."""
    assert quote in passage
    case = vc.text_case("Net sales changed this year.", quote, passage=passage)
    assert_fails(run(case), "quote_substring")
    assert locate_quote(quote, passage) is None


@pytest.mark.parametrize(
    ("passage", "quote"),
    [
        ("Net sales change was (6)% this year.", "(6)% this year"),
        ("Net sales changed -3% this year.", "changed -3% this year"),
        ("EPS guidance is $9.90 - $10.00 for the year.", "$10.00 for the year"),
    ],
    ids=["whole_parenthesis", "whole_minus", "spaced_range_hyphen"],
)
def test_a_quote_holding_the_whole_number_is_kept(passage: str, quote: str) -> None:
    """A spaced hyphen in a range is not a sign."""
    case = vc.text_case("Guidance was reported.", quote, passage=passage)
    assert run(case).checks.quote_substring == "pass"


# --- G: the sign a quote's wording carries -------------------------------------------------------


@pytest.mark.parametrize(
    ("claim_text", "quote"),
    [
        ("Net sales changed by -3% during 2023.", SALES_QUOTE),  # "decreased 3%"
        ("Sales showed a 6% decrease.", "Sales change versus prior year (6)%"),
        ("Sales decreased 6%.", "Sales change versus prior year (6)%"),
        ("Sales changed by -3% this year.", "Sales were down 3% this year"),
    ],
)
def test_decline_wording_and_parentheses_read_as_negative(claim_text: str, quote: str) -> None:
    passage = f"Overview. {quote}. End."
    assert_passes(run(vc.text_case(claim_text, quote, passage=passage)))


@pytest.mark.parametrize(
    ("claim_text", "quote"),
    [
        ("Net sales increased 3% during 2023.", SALES_QUOTE),
        ("Sales increased 6%.", "Sales change versus prior year (6)%"),
        ("Sales changed by -3% this year.", "Sales increased 3% this year"),
    ],
)
def test_a_clear_sign_contradiction_with_the_quote_fails(claim_text: str, quote: str) -> None:
    passage = f"Overview. {quote}. End."
    assert_fails(run(vc.text_case(claim_text, quote, passage=passage)), "number_in_text")


# --- H1: a text claim states no value of its own -------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{"value": "999000000000"}, {"display": "$999 billion"}, {"value": "3", "display": "3%"}],
    ids=["value", "display", "both"],
)
def test_a_text_claim_carrying_a_value_or_display_fails(extra: dict[str, str]) -> None:
    """Decision H1: nothing checks those fields against the quote, so a text claim may not
    carry them, even one that happens to agree."""
    case = vc.text_case("Net sales decreased 3% during 2023.", SALES_QUOTE)
    case = case._replace(claim=vc.edit(case.claim, **extra))
    assert_fails(run(case), "number_in_text", others_pass=("quote_substring", "metadata_match"))


# --- company names are not checked in text claims ------------------------------------------------


def test_a_text_claim_is_not_judged_by_the_companies_it_names() -> None:
    """The company-name rule applies to numeric claims only; a text claim's company check is
    its passage's cik. DOCUMENTS A LIMIT: this claim attributes Apple's passage to Microsoft
    and is kept."""
    case = vc.text_case(
        "Microsoft's net sales decreased 3% during 2023.", SALES_QUOTE, allowed=APPLE_ONLY
    )
    assert_passes(run(vc.with_real_companies(case)))


@pytest.mark.parametrize(
    "claim_text",
    ["Sales increased 5% this year.", "Costs decreased 3% this year."],
)
def test_a_direction_word_in_a_quote_binds_to_its_own_number(claim_text: str) -> None:
    """Decision G: one decline word does not make every number in the quote negative."""
    quote = "Sales increased 5% while costs decreased 3% this year"
    assert_passes(run(vc.text_case(claim_text, quote, passage=f"Overview. {quote}.")))


def test_dev_fb_01328_c2_is_dropped_for_its_bare_table_number_only() -> None:
    """fb-01328 c2 writes "411" for a cell in a table declared "(in millions)": 411 is not
    411 million, so it is dropped. DOCUMENTS A LIMIT all the same: its over-claim (that the
    charge is absent from the income statement) is not what is detected."""
    outcome = run(vc.dev_case("fb-01328", "c2"))
    assert_fails(outcome, "number_in_text", others_pass=("quote_substring", "metadata_match"))


@pytest.mark.parametrize(
    ("claim_text", "expected"),
    [
        ("Current liabilities were 5,466,312 (in thousands).", "pass"),
        ("Current liabilities were 5,466,312 (in millions).", "fail"),
    ],
)
def test_a_scale_in_parentheses_after_a_claim_number_is_its_scale(
    claim_text: str, expected: str
) -> None:
    quote = "Total current liabilities | 5,466,312 | 4,586,657"
    case = vc.text_case(claim_text, quote, passage=f"(in thousands, except share data) {quote}")
    assert run(case).checks.number_in_text == expected


def test_a_quoted_sign_counts_without_any_direction_word() -> None:
    """3 is not -3: only direction wording lets an unsigned magnitude state a negative."""
    quote = "Net sales changed -3% this year"
    case = vc.text_case("Sales changed by 3% this year.", quote, passage=f"Overview. {quote}.")
    assert_fails(run(case), "number_in_text", others_pass=("quote_substring",))
