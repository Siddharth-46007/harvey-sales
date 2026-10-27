"""The contact extractor's resume marker.

The extractor used to resume on "this company has no prospects yet", which
breaks the moment another source writes a prospect row: the CBSE import
stores a principal for every school it imports, so every imported school
looked already-extracted and was skipped. The marker is now the extractor's
own, and it is set on every attempt — a site that publishes no address must
not be re-fetched on every pass.
"""

import tempfile
from pathlib import Path

import pytest

from harvey.collectors.contacts import ContactExtractor
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


async def _state(tmp):
    state = StateManager(str(Path(tmp) / "harvey.db"))
    await state.init_db()
    return state


async def _marker(state, company_id):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
                "SELECT contacts_checked_at, phone FROM companies WHERE id = ?",
                (company_id,)) as cur:
            return await cur.fetchone()


@pytest.mark.asyncio
async def test_a_directory_principal_does_not_mark_a_site_as_read():
    with tempfile.TemporaryDirectory() as tmp:
        state = await _state(tmp)
        cid = await state.add_company(Company(
            name="A SCHOOL", domain="a.example", source="CBSE directory"))
        await state.add_prospect(Prospect(
            company_id=cid, company="A SCHOOL", first_name="Anita",
            title="Principal", source="CBSE directory"))

        checked, _phone = await _marker(state, cid)
        assert checked is None


@pytest.mark.asyncio
async def test_marker_is_set_even_when_nothing_was_found():
    with tempfile.TemporaryDirectory() as tmp:
        state = await _state(tmp)
        cid = await state.add_company(Company(
            name="B SCHOOL", domain="b.example", source="CBSE directory"))

        await state.mark_contacts_extracted(cid)
        checked, _phone = await _marker(state, cid)
        assert checked is not None


@pytest.mark.asyncio
async def test_a_scraped_phone_fills_a_blank():
    with tempfile.TemporaryDirectory() as tmp:
        state = await _state(tmp)
        cid = await state.add_company(Company(
            name="C SCHOOL", domain="c.example", source="CBSE directory"))

        await state.mark_contacts_extracted(cid, "+919845012345")
        _checked, phone = await _marker(state, cid)
        assert phone == "+919845012345"


@pytest.mark.asyncio
async def test_a_scraped_phone_never_replaces_one_already_recorded():
    """A number from a directory or a human beats one read out of a footer."""
    with tempfile.TemporaryDirectory() as tmp:
        state = await _state(tmp)
        cid = await state.add_company(Company(
            name="D SCHOOL", domain="d.example", source="CBSE directory",
            phone="+918023456789"))

        await state.mark_contacts_extracted(cid, "+919845012345")
        _checked, phone = await _marker(state, cid)
        assert phone == "+918023456789"


@pytest.mark.asyncio
async def test_run_one_marks_the_attempt_and_stores_the_lead_phone(monkeypatch):
    """A site with a phone number and no address is still worth recording."""
    with tempfile.TemporaryDirectory() as tmp:
        state = await _state(tmp)
        cid = await state.add_company(Company(
            name="E SCHOOL", domain="e.example", source="CBSE directory"))

        page = ('<a href="tel:+918023456789">office</a>'
                '<p>Also on 98450 12345</p>')
        extractor = ContactExtractor(state, config=None)
        monkeypatch.setattr(
            ContactExtractor, "_pages",
            lambda self, client, domain: _one_page(page, domain))

        result = await extractor.run_one(None, {
            "id": cid, "name": "E SCHOOL", "domain": "e.example",
            "phone": "", "industry": "school"})

        assert result["emails"] == 0
        assert result["phones"] == 2
        assert result["phone_stored"] == 1
        checked, phone = await _marker(state, cid)
        assert checked is not None
        # The tel: link is the site's own assertion, so it is the one stored.
        assert phone == "+918023456789"


async def _one_page(html, domain):
    return [(f"https://{domain}", html)]
