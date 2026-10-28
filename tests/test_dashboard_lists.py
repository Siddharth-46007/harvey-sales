"""Paginated, filterable Companies and Contacts, and the coverage panel.

A directory import puts thousands of rows in these tables, so "the newest
200" silently hides most of the pipeline. These cover the contract the UI
depends on: every row is reachable by paging, every filter narrows both the
view and its export, and the coverage panel's credit figure comes from
Harvey's own run log rather than an API call that would itself cost one.
"""

import csv
import io
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.geography import backfill_geography
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


SCHOOLS = [
    # name, domain, location, source, phone, profiled
    ("ST MARYS SCHOOL", "stmarys.example", "BANGALORE URBAN, KARNATAKA",
     "CBSE directory", "+918023456789", True),
    ("GREENWOOD HIGH", "greenwood.example", "Indiranagar, Bengaluru, India",
     "serper_places", "+919845012345", True),
    ("OAKRIDGE IB WORLD SCHOOL", "oakridge.example", "HYDERABAD, TELANGANA",
     "CBSE directory", "", False),
    ("GULF INDIAN SCHOOL", "", "BAHRAIN", "CBSE directory", "+97312345678",
     False),
]

CONTACTS = [
    # school index, email, status, score, first, title, source
    (0, "principal@stmarys.example", "published", 100, "", "Principal",
     "found on website"),
    (0, "hr@stmarys.example", "published", 60, "", "HR", "found on website"),
    (1, "office.greenwood@gmail.com", "published_freemail", 100, "",
     "School Office", "found on website (free-mail)"),
    (2, "", "", 40, "Anita", "Principal", "CBSE directory"),
    (3, "", "", 40, "Rahul", "Principal", "CBSE directory"),
]


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(
            dash, "WEB_DIR",
            Path(__file__).resolve().parent.parent / "harvey" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()

        import asyncio

        async def seed():
            state = StateManager(str(db))
            await state.init_db()
            ids = []
            for name, domain, loc, source, phone, profiled in SCHOOLS:
                cid = await state.add_company(Company(
                    name=name, domain=domain, location=loc, source=source,
                    phone=phone, industry="school"))
                ids.append(cid)
                if profiled:
                    await state.mark_company_profiled(cid)
            for i, email, status, score, first, title, src in CONTACTS:
                await state.add_prospect(Prospect(
                    company_id=ids[i], company=SCHOOLS[i][0],
                    first_name=first, title=title, email=email,
                    email_status=status, score=score, source=src))
            # Two of the four sites have been read by the extractor.
            await state.mark_contacts_extracted(ids[0])
            await state.mark_contacts_extracted(ids[1])
            await backfill_geography(state)

        asyncio.run(seed())

        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            yield c


# ── Pagination ──


def test_contacts_are_paged_and_every_row_is_reachable(client):
    seen = []
    page = 1
    while True:
        d = client.get(f"/api/prospects?per_page=2&page={page}").json()
        assert d["per_page"] == 2
        seen += [r["id"] for r in d["rows"]]
        if page >= d["pages"]:
            assert d["total"] == len(CONTACTS)
            break
        page += 1
    assert len(set(seen)) == len(CONTACTS)


def test_companies_are_paged(client):
    d = client.get("/api/companies?per_page=2&page=2").json()
    assert d["total"] == len(SCHOOLS) and d["pages"] == 2
    assert len(d["rows"]) == 2


def test_per_page_is_capped_so_one_request_cannot_pull_everything(client):
    assert client.get("/api/prospects?per_page=9999").json()["per_page"] <= 200
    assert client.get("/api/companies?per_page=9999").json()["per_page"] <= 200


def test_a_page_past_the_end_is_empty_not_an_error(client):
    d = client.get("/api/prospects?page=99").json()
    assert d["rows"] == [] and d["total"] == len(CONTACTS)


# ── "Showing X of Y" ──


def test_unfiltered_view_shows_the_whole_table(client):
    d = client.get("/api/prospects").json()
    assert d["total"] == d["grand_total"] == len(CONTACTS)
    d = client.get("/api/companies").json()
    assert d["total"] == d["grand_total"] == len(SCHOOLS)


def test_a_filtered_view_still_reports_the_table_total(client):
    """X is what matches, Y is everything — so a narrowed view says what it
    is hiding."""
    d = client.get("/api/prospects?state_region=Karnataka").json()
    assert d["total"] == 3
    assert d["grand_total"] == len(CONTACTS)

    d = client.get("/api/companies?has_website=no").json()
    assert d["total"] == 1
    assert d["grand_total"] == len(SCHOOLS)


def test_the_count_is_the_whole_result_not_the_page(client):
    """Paging must not change either number."""
    d = client.get("/api/prospects?per_page=2&page=1").json()
    assert len(d["rows"]) == 2
    assert d["total"] == len(CONTACTS)
    assert d["grand_total"] == len(CONTACTS)


def test_a_search_that_matches_nothing_reports_zero_of_the_total(client):
    d = client.get("/api/prospects?q=nothing-here").json()
    assert d["total"] == 0 and d["grand_total"] == len(CONTACTS)
    d = client.get("/api/companies?q=nothing-here").json()
    assert d["total"] == 0 and d["grand_total"] == len(SCHOOLS)


# ── Contact filters ──


def test_filter_by_state_reaches_directory_and_discovery_rows_alike(client):
    """"Karnataka" has to find both the "…, KARNATAKA" directory row and the
    "Indiranagar, Bengaluru, India" discovery row."""
    d = client.get("/api/prospects?state_region=Karnataka").json()
    assert d["total"] == 3
    assert {r["company"] for r in d["rows"]} == {
        "ST MARYS SCHOOL", "GREENWOOD HIGH"}


def test_filter_by_board(client):
    d = client.get("/api/prospects?board=CBSE").json()
    assert all(r["board"] == "CBSE" for r in d["rows"])
    assert d["total"] == 4


def test_filter_has_email(client):
    assert client.get("/api/prospects?has_email=yes").json()["total"] == 3
    assert client.get("/api/prospects?has_email=no").json()["total"] == 2


def test_primary_only_returns_one_address_per_school(client):
    d = client.get("/api/prospects?primary_only=1").json()
    assert d["total"] == 2
    assert "hr@stmarys.example" not in {r["email"] for r in d["rows"]}


def test_filter_by_email_status(client):
    d = client.get("/api/prospects?email_status=published_freemail").json()
    assert d["total"] == 1
    assert d["rows"][0]["email"] == "office.greenwood@gmail.com"


def test_filter_by_source(client):
    d = client.get("/api/prospects?source=CBSE directory").json()
    assert d["total"] == 2


@pytest.mark.parametrize("q,expected", [
    ("greenwood", 1),          # school name
    ("principal@", 1),         # email
    ("Anita", 1),              # principal's name
    ("nothing-here", 0),
])
def test_contact_search(client, q, expected):
    assert client.get(f"/api/prospects?q={q}").json()["total"] == expected


@pytest.mark.parametrize("sort", ["school", "city", "added", "score"])
def test_contact_sorts_are_accepted(client, sort):
    d = client.get(f"/api/prospects?sort={sort}").json()
    assert d["total"] == len(CONTACTS)


def test_sort_by_school_name_is_alphabetical(client):
    rows = client.get("/api/prospects?sort=school").json()["rows"]
    names = [r["company"] for r in rows]
    assert names == sorted(names)


def test_an_unknown_sort_falls_back_rather_than_erroring(client):
    r = client.get("/api/prospects?sort=; DROP TABLE prospects")
    assert r.status_code == 200
    assert r.json()["total"] == len(CONTACTS)


# ── Company filters ──


def test_company_filter_has_website(client):
    assert client.get("/api/companies?has_website=yes").json()["total"] == 3
    assert client.get("/api/companies?has_website=no").json()["total"] == 1


def test_company_filter_has_phone(client):
    assert client.get("/api/companies?has_phone=yes").json()["total"] == 3


def test_company_filter_has_email(client):
    d = client.get("/api/companies?has_email=yes").json()
    assert d["total"] == 2
    assert all(r["email_count"] > 0 for r in d["rows"])


def test_company_filter_profiled(client):
    assert client.get("/api/companies?profiled=yes").json()["total"] == 2
    assert client.get("/api/companies?profiled=no").json()["total"] == 2


def test_company_filter_by_country(client):
    d = client.get("/api/companies?country=Bahrain").json()
    assert d["total"] == 1 and d["rows"][0]["name"] == "GULF INDIAN SCHOOL"


def test_company_search(client):
    assert client.get("/api/companies?q=oakridge").json()["total"] == 1


def test_company_rows_carry_their_contact_counts(client):
    d = client.get("/api/companies?q=stmarys").json()
    assert d["rows"][0]["contact_count"] == 2
    assert d["rows"][0]["email_count"] == 2


# ── Filter options ──


def test_filter_options_come_with_counts(client):
    f = client.get("/api/contacts/filters").json()
    states = {r["value"]: r["n"] for r in f["state_region"]}
    assert states["Karnataka"] == 2 and states["Telangana"] == 1
    assert {r["value"] for r in f["board"]} >= {"CBSE"}
    assert any(r["value"] == "CBSE directory" for r in f["company_source"])


# ── Export matches the view ──


def test_export_applies_the_same_filters_as_the_view(client):
    r = client.get("/api/export/contacts.csv?state_region=Karnataka"
                   "&has_email=yes")
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 3
    assert {row["state_region"] for row in rows} == {"Karnataka"}
    assert all(row["email"] for row in rows)


def test_export_is_not_capped_at_a_page(client):
    r = client.get("/api/export/contacts.csv?per_page=2")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == len(CONTACTS)


def test_export_header_names_the_columns_a_sequencer_needs(client):
    r = client.get("/api/export/contacts.csv")
    header = r.text.splitlines()[0]
    for col in ("school", "city", "state_region", "country", "board",
                "website", "phone", "email", "email_status", "source_url"):
        assert col in header


# ── Coverage panel ──


def test_progress_totals(client):
    t = client.get("/api/progress").json()["totals"]
    assert t["schools"] == 4
    assert t["with_website"] == 3
    assert t["with_email"] == 2
    assert t["emails"] == 3
    assert t["primary_emails"] == 2
    assert t["principals_named"] == 2
    assert t["extracted"] == 2
    # Only a school with a website can be pending: the Bahrain row has none.
    assert t["extraction_pending"] == 1


def test_progress_by_region_separates_read_from_pending(client):
    regions = {r["region"]: r for r in
               client.get("/api/progress").json()["by_region"]}
    assert regions["Karnataka"]["schools"] == 2
    assert regions["Karnataka"]["extracted"] == 2
    assert regions["Karnataka"]["pending"] == 0
    # Hyderabad's site has never been read.
    assert regions["Telangana"]["pending"] == 1
    # An overseas school with no website is listed under its country.
    assert regions["Bahrain"]["schools"] == 1


def test_a_directory_principal_does_not_count_as_an_extracted_site(client):
    """The CBSE import stores a principal for every school. If "has a
    prospect" meant "extracted", every imported school would look done and
    the extractor would skip all of them."""
    regions = {r["region"]: r for r in
               client.get("/api/progress").json()["by_region"]}
    assert regions["Telangana"]["extracted"] == 0


def test_credits_come_from_the_local_run_log(client):
    c = client.get("/api/progress").json()["credits"]
    assert "no API call" in c["source"]
    assert c["used"] == 0
    assert c["remaining"] == c["free_allowance"]
    assert c["caveat"]
