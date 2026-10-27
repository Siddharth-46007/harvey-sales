"""Phone numbers read out of a page's visible text.

A school footer carries an affiliation number, a UDISE code, a pincode, a
year range and sometimes a bank account alongside the phone number. These
cover the rule that a candidate has to prove itself by shape and context —
a wrong phone number on a prospect row is worse than a blank one.
"""

import pytest

from harvey.collectors.contacts import (
    extract_text_phones,
    normalise_phone,
)


# ── normalise_phone ──


@pytest.mark.parametrize("raw,expected", [
    # Indian mobiles, in the shapes sites actually print.
    ("+91 98450 12345", "+919845012345"),
    ("9845012345", "+919845012345"),
    ("09845012345", "+919845012345"),
    ("919845012345", "+919845012345"),
    ("(+91) 98450-12345", "+919845012345"),
    # A landline with its STD code: the trunk 0 drops, +91 goes on.
    ("080-23456789", "+918023456789"),
    # International numbers keep their own country code.
    ("+971 4 123 4567", "+97141234567"),
    ("+44 20 7123 4567", "+442071234567"),
])
def test_recognised_numbers(raw, expected):
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "560038",                 # pincode
    "830221",                 # CBSE affiliation number
    "0000000000",             # placeholder
    "1111122222",             # placeholder
    "1234567890",             # ascending run
    "2024202520262027",       # a row of years
    "12345",                  # too short
    "",
])
def test_rejected_numbers(raw):
    assert normalise_phone(raw) == ""


def test_two_indian_numbers_run_together_take_the_first():
    assert normalise_phone("98450123459900112233") == "+919845012345"


def test_two_international_numbers_run_together_are_dropped_not_mangled():
    """With a leading +, the country code sets the length — guessing a
    boundary would invent a number that dials somewhere else."""
    assert normalise_phone("+971501234567+97142223333") == ""


# ── extract_text_phones ──


def test_finds_numbers_that_are_not_in_a_tel_link():
    html = "<p>Call us on +91 98450 12345 or 080-2345 6789</p>"
    assert extract_text_phones(html) == ["+919845012345", "+918023456789"]


def test_ignores_script_and_style():
    html = """<style>.x{width:98450123456px}</style>
    <script>var build=20240101123456; var n="9900112233";</script>
    <p>Phone: +91 98450 12345</p>"""
    assert extract_text_phones(html) == ["+919845012345"]


@pytest.mark.parametrize("label", [
    "Affiliation No.", "UDISE Code:", "School Code -", "GSTIN", "CIN",
    "Account No.", "Registration No", "Pin Code", "Estd", "Fees: Rs.",
])
def test_a_labelled_number_is_not_a_phone(label):
    assert extract_text_phones(f"<p>{label} 2928010011234</p>") == []


def test_a_labelled_number_does_not_suppress_a_real_one_elsewhere():
    html = ("<div>Affiliation No. 830221</div>"
            "<div>Phone: +91 98450 12345</div>")
    assert extract_text_phones(html) == ["+919845012345"]


def test_entities_and_nbsp_do_not_break_a_number():
    html = "<p>Tel:&nbsp;&#43;91&nbsp;98450&nbsp;12345</p>"
    assert extract_text_phones(html) == ["+919845012345"]


def test_duplicates_collapse_and_page_order_is_kept():
    html = ("<p>+91 98450 12345</p><p>9900112233</p>"
            "<footer>9845012345</footer>")
    assert extract_text_phones(html) == ["+919845012345", "+919900112233"]


def test_limit_caps_what_one_page_can_contribute():
    html = "".join(f"<p>+91 9845{i:06d}</p>" for i in range(30))
    assert len(extract_text_phones(html, limit=5)) == 5


# ── how the extractor combines the two sources ──


def test_tel_link_and_text_dedupe_to_one_number():
    """The same number in a tel: link and in the footer is one number."""
    from harvey.collectors.contacts import ContactExtractor
    html = ('<a href="tel:9845012345">call</a>'
            '<p>Phone: +91 98450 12345</p>')
    _emails, phones = ContactExtractor._harvest(html)
    assert phones == ["+919845012345"]


def test_tel_link_comes_first():
    """A tel: link is the site asserting its own number, so it leads."""
    from harvey.collectors.contacts import ContactExtractor
    html = ('<p>Also on 9900112233</p>'
            '<a href="tel:+918023456789">office</a>')
    _emails, phones = ContactExtractor._harvest(html)
    assert phones[0] == "+918023456789"
    assert "+919900112233" in phones
