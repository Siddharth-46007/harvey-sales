"""Parsing a source's free-text location into country / state / city.

Every collector writes ``companies.location`` in its own shape. These cover
the shapes Harvey actually stores, and the rule that an unknown piece comes
back blank rather than guessed.
"""

import tempfile
from pathlib import Path

import pytest

from harvey.geography import backfill_geography, parse_board, parse_location
from harvey.models.company import Company
from harvey.state import StateManager


# ── parse_location ──


@pytest.mark.parametrize("label,expected", [
    # Locality discovery: "<hood>, <city>, <country>".
    ("Indiranagar, Bengaluru, India", ("India", "Karnataka", "Bengaluru")),
    # CBSE directory, Indian school: "<DISTRICT>, <STATE>".
    ("BANGALORE URBAN, KARNATAKA", ("India", "Karnataka", "Bangalore Urban")),
    ("HYDERABAD, TELANGANA", ("India", "Telangana", "Hyderabad")),
    # CBSE directory, overseas school: the country alone.
    ("BAHRAIN", ("Bahrain", "", "")),
    # Older discovery rows that never recorded a country.
    ("Porur, Chennai", ("India", "Tamil Nadu", "Chennai")),
    # A Gulf city names its own country.
    ("DUBAI", ("UAE", "", "Dubai")),
    ("Al Barsha, Dubai, UAE", ("UAE", "", "Dubai")),
    # A state on its own is a state, not a city.
    ("KARNATAKA", ("India", "Karnataka", "")),
    ("GOA", ("India", "Goa", "")),
    # Both at once: Chandigarh is a union territory and a city.
    ("CHANDIGARH, CHANDIGARH", ("India", "Chandigarh", "Chandigarh")),
    ("", ("", "", "")),
])
def test_parse_location_shapes(label, expected):
    assert parse_location(label) == expected


def test_unknown_place_is_blank_not_guessed():
    """A wrong country is worse than no country."""
    country, state, city = parse_location("Somewhere, Nowheresville")
    assert (country, state) == ("", "")
    assert city == "Nowheresville"


def test_shouted_directory_values_are_tidied_for_display():
    assert parse_location("SOUTH WEST DELHI, DELHI")[2] == "South West Delhi"


def test_already_cased_city_is_left_alone():
    assert parse_location("Whitefield, Bengaluru, India")[2] == "Bengaluru"


# ── parse_board ──


def test_cbse_directory_rows_are_cbse_by_definition():
    assert parse_board(source="CBSE directory") == "CBSE"


@pytest.mark.parametrize("industry,name,expected", [
    ("ICSE School", "St Xavier's", "ICSE/ISC"),
    ("", "Oakridge IB World School", "IB"),
    ("Cambridge IGCSE", "", "IGCSE/Cambridge"),
    ("state board school", "", "State board"),
    ("school", "Greenwood High", ""),
])
def test_board_from_what_the_row_says(industry, name, expected):
    assert parse_board("serper_places", industry, name) == expected


# ── backfill ──


async def _row(state):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT location, country, state_region, city, board "
                "FROM companies LIMIT 1") as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_backfill_fills_blanks_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(str(Path(tmp) / "harvey.db"))
        await state.init_db()
        await state.add_company(Company(
            name="A SCHOOL", domain="a.example",
            location="BANGALORE URBAN, KARNATAKA", source="CBSE directory"))

        assert (await backfill_geography(state))["rows"] == 1

        row = await _row(state)
        assert (row["country"], row["state_region"], row["city"],
                row["board"]) == ("India", "Karnataka", "Bangalore Urban",
                                  "CBSE")
        # The verbatim provenance label is never rewritten.
        assert row["location"] == "BANGALORE URBAN, KARNATAKA"

        assert (await backfill_geography(state))["rows"] == 0


@pytest.mark.asyncio
async def test_backfill_never_overwrites_a_corrected_value():
    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(str(Path(tmp) / "harvey.db"))
        await state.init_db()
        await state.add_company(Company(
            name="B SCHOOL", domain="b.example",
            location="BANGALORE URBAN, KARNATAKA", source="CBSE directory"))
        await backfill_geography(state)

        import aiosqlite
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute("UPDATE companies SET city = 'Bengaluru'")
            await db.commit()

        await backfill_geography(state)
        assert (await _row(state))["city"] == "Bengaluru"
