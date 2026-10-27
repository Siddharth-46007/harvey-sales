"""Importer for the CBSE affiliated-schools directory (SARAS 7.0).

Bulk coverage for free. CBSE publishes every affiliated school in India plus
its overseas schools, and the list view alone carries name, state, district
(country, for overseas), affiliation level, postal address, website and the
principal's name. One POST per state returns that state's entire list, so the
whole directory — 33,000+ schools — costs about 40 requests.

What it does NOT provide: email and phone. Neither appears in the list or on
the per-school detail page, so there is nothing to scrape for them. The
intended pairing is this importer for coverage, then
``harvey contacts`` (also free) to read published addresses off the websites
this gives us.

Terms: CBSE's Website Policy permits reproduction of site material free of
charge without specific permission, provided it is reproduced accurately,
not used misleadingly, and the source is acknowledged. Every row is therefore
stored with ``source = "CBSE directory"``, and requests are paced well below
anything that would burden the portal.
"""

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.collectors.discover import (compile_excludes, is_excluded,
                                        is_junk, normalize_domain, Business)

logger = logging.getLogger("harvey.cbse")

BASE = "https://saras.cbse.gov.in"
LIST_URL = f"{BASE}/saras/AffiliatedList/ListOfSchdirReport"
DETAIL_URL = f"{BASE}/saras/AffiliatedList/AfflicationDetails/{{}}"

# A government portal serving a public directory. One request every few
# seconds is slower than a single human clicking through it.
REQUEST_INTERVAL = 3.0
UA = ("Mozilla/5.0 (compatible; Harvey/0.3; +https://github.com/"
      "ethanplusai/harvey) cbse-directory-import")

TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')

# Each list cell packs several labelled values into one string.
AFF_RE = re.compile(r"Aff\.\s*No\.\s*:\s*(\S+)", re.I)
CODE_RE = re.compile(r"Sch\.\s*Code:\s*(\S+)", re.I)
STATE_RE = re.compile(r"State\s*:\s*(.*?)\s*District\s*:", re.I)
DISTRICT_RE = re.compile(r"District\s*:\s*(.*)$", re.I)
NAME_RE = re.compile(r"Name\s*:\s*(.*?)\s*Head/Principal\s*Name\s*:", re.I)
PRINCIPAL_RE = re.compile(r"Head/Principal\s*Name\s*:\s*(.*)$", re.I)
ADDRESS_RE = re.compile(r"Address\s*:\s*(.*?)(?:\s*Website\s*:|$)", re.I)
WEBSITE_RE = re.compile(r"Website\s*:\s*(\S+)\s*$", re.I)
# Honorifics carry no information and wreck first/last splitting.
HONORIFIC_RE = re.compile(
    r"^(mr|mrs|ms|miss|dr|prof|smt|shri|sri|sh|fr|sr|rev|capt|col|maj)\.?\s+",
    re.I)


def _txt(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def parse_rows(html: str) -> list[dict]:
    """Every school in one list response."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        ident, place, level = _txt(tds[1]), _txt(tds[2]), _txt(tds[3])
        who, where = _txt(tds[4]), _txt(tds[5])

        aff = (AFF_RE.search(ident) or [None, ""])[1] if AFF_RE.search(ident) \
            else ""
        if not aff:
            continue
        name_m = NAME_RE.search(who)
        name = name_m.group(1).strip() if name_m else who.replace("Name :", "").strip()
        if not name:
            continue
        website_m = WEBSITE_RE.search(where)
        website = website_m.group(1).strip() if website_m else ""
        if website.lower() in ("", "na", "n/a", "-", "nil", "none"):
            website = ""
        addr_m = ADDRESS_RE.search(where)
        code_m = CODE_RE.search(ident)
        st_m, di_m = STATE_RE.search(place), DISTRICT_RE.search(place)
        pr_m = PRINCIPAL_RE.search(who)

        out.append({
            "aff_no": aff,
            "school_code": code_m.group(1).strip() if code_m else "",
            "state": st_m.group(1).strip() if st_m else "",
            "district": di_m.group(1).strip() if di_m else "",
            "level": level,
            "name": name,
            "principal": (pr_m.group(1).strip() if pr_m else ""),
            "address": (addr_m.group(1).strip() if addr_m else ""),
            "website": website,
            "detail_url": DETAIL_URL.format(aff),
        })
    return out


def split_principal(raw: str) -> tuple[str, str]:
    """"MRS KUMUBINI LAHIRI" -> ("Kumubini", "Lahiri")."""
    s = HONORIFIC_RE.sub("", (raw or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .,")
    if not s or s.lower() in ("na", "n/a", "nil", "vacant", "-"):
        return "", ""
    parts = [p.capitalize() if p.isupper() else p for p in s.split()]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


class CBSEImporter:
    """Walks the SARAS directory state by state and stores what it finds."""

    def __init__(self, state, config, interval: float = REQUEST_INTERVAL):
        self.state = state
        self.config = config
        self.interval = max(1.0, interval)
        self.excludes = compile_excludes(
            getattr(config.icp, "exclude_patterns", []))
        self._token = ""

    async def _open(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Fetch the form, keep its antiforgery token, return {state: id}."""
        r = await client.get(LIST_URL, headers={"User-Agent": UA}, timeout=45)
        r.raise_for_status()
        m = TOKEN_RE.search(r.text)
        if not m:
            raise RuntimeError(
                "CBSE: no __RequestVerificationToken on the list page — the "
                "portal's form has changed; the importer needs updating.")
        self._token = m.group(1)
        soup = BeautifulSoup(r.text, "html.parser")
        sel = soup.find("select", id="State")
        states = {}
        for opt in (sel.find_all("option") if sel else []):
            val = (opt.get("value") or "").strip()
            label = opt.get_text(strip=True)
            if val and label and not label.startswith("--"):
                states[label] = val
        if not states:
            raise RuntimeError("CBSE: could not read the State dropdown.")
        return states

    async def _fetch_state(self, client: httpx.AsyncClient,
                           state_id: str) -> list[dict]:
        r = await client.post(
            LIST_URL,
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"__RequestVerificationToken": self._token,
                  "__Invariant": "RegiAffNo",
                  "State": state_id, "District": "",
                  "Region": "", "SchoolStatusWise": "1"},
            timeout=120,
        )
        r.raise_for_status()
        return parse_rows(r.text)

    async def _store(self, row: dict) -> str:
        """Insert one school and its principal. Returns 'new'/'known'/'skip'."""
        domain = normalize_domain(row["website"]) if row["website"] else ""
        # The ICP's own exclusions (government networks, coaching, preschool
        # only) decide what is worth keeping — same rules as discovery.
        biz = Business(name=row["name"], raw_title=row["name"], domain=domain,
                       category=row["level"], address=row["address"],
                       location=f'{row["district"]}, {row["state"]}')
        if is_junk(domain, row["name"], biz.location) or \
                is_excluded(biz, self.excludes):
            return "skip"

        external_id = f'cbse:{row["aff_no"]}'
        existing = None
        if domain:
            existing = await self.state.get_company_by_domain(domain)
        if existing is None:
            existing = await self.state.get_company_by_external_id(external_id)
        if existing is not None:
            return "known"

        location = (row["district"] if row["state"] == "FOREIGN SCHOOLS"
                    else f'{row["district"]}, {row["state"]}')
        company_id = await self.state.add_company(Company(
            name=row["name"],
            domain=domain,
            website=(f'https://{domain}' if domain else ""),
            industry=row["level"] or "CBSE school",
            location=location.strip(", "),
            source="CBSE directory",
            source_url=row["detail_url"],
            external_id=external_id,
            notes=(f'CBSE affiliation {row["aff_no"]}'
                   + (f', school code {row["school_code"]}'
                      if row["school_code"] else "")
                   + (f'. {row["address"]}' if row["address"] else "")),
        ))

        first, last = split_principal(row["principal"])
        if first:
            # No email: CBSE publishes none. The name and title are still the
            # decision-maker, and `harvey contacts` can attach an address to
            # them later once the school's own site has been read.
            await self.state.add_prospect(Prospect(
                company_id=company_id,
                first_name=first, last_name=last,
                title="Principal", seniority="c_suite",
                email="", email_status="",
                source="CBSE directory",
                source_url=row["detail_url"],
                company=row["name"],
                industry=row["level"] or "CBSE school",
                personalization_notes=(
                    f'Principal per the CBSE affiliated-schools directory '
                    f'(affiliation {row["aff_no"]}).'),
            ))
        return "new"

    async def run(self, states: list[str] | None = None,
                  dry_run: bool = False) -> dict:
        t = {"states": 0, "rows": 0, "new": 0, "known": 0, "skipped": 0,
             "principals": 0, "errors": []}
        async with httpx.AsyncClient(follow_redirects=True) as client:
            available = await self._open(client)
            wanted = ([s for s in available if not states
                       or s.upper() in {x.upper() for x in states}])
            if dry_run:
                t["states"] = len(wanted)
                t["plan"] = wanted
                return t

            for i, label in enumerate(wanted):
                if i:
                    await asyncio.sleep(self.interval)
                try:
                    rows = await self._fetch_state(client, available[label])
                except Exception as e:
                    logger.warning("CBSE: %s failed: %s", label, e)
                    t["errors"].append(f"{label}: {e}")
                    continue
                t["states"] += 1
                t["rows"] += len(rows)
                for row in rows:
                    try:
                        verdict = await self._store(row)
                    except Exception as e:
                        logger.debug("CBSE: %s not stored: %s",
                                     row.get("name"), e)
                        continue
                    if verdict == "new":
                        t["new"] += 1
                        if row["principal"]:
                            t["principals"] += 1
                    elif verdict == "known":
                        t["known"] += 1
                    else:
                        t["skipped"] += 1
                logger.info("CBSE: %s — %d rows (%d new so far)",
                            label, len(rows), t["new"])
        return t
