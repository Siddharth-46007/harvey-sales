"""Deterministic contact extraction from a company's own website.

No model calls, no search API, no paid lookups: this reads pages the business
already publishes and takes the addresses it finds there verbatim. That makes
it the cheapest contact source available and the only one whose output needs
no verification caveat — a published address is a fact about the site, not a
guess about a pattern.

Deliberately NOT here:
  * pattern guessing (first.last@domain). A guess needs a verifier to be
    worth anything, and this collector is for the case where there is none.
  * search engines. Every page fetched belongs to the company itself.
  * Claude. Classification is a regex and a word list; a model adds latency
    and quota for no accuracy.

What it stores: one prospect per published address, ``source`` set to
"found on website" and ``source_url`` to the page it appeared on, so every
row can be audited back to a URL. Addresses are marked ``published``, which
is not a sendable status — the pre-send gate only passes verified/risky, so
nothing here can be emailed by accident.
"""

import asyncio
import logging
import re
from urllib.parse import urljoin

import httpx

from harvey.models.prospect import Prospect

logger = logging.getLogger("harvey.contacts")

# Pages worth reading, in the order a human would try them. One page per
# group is enough: schools repeat the same address in the footer anyway.
PAGE_GROUPS = (
    ("home", ("",)),
    ("contact", ("/contact", "/contact-us", "/contactus", "/reach-us",
                 "/get-in-touch", "/contact.html", "/contact.php")),
    ("about", ("/about", "/about-us", "/aboutus", "/about.html")),
    ("admissions", ("/admissions", "/admission", "/admission-enquiry",
                    "/apply", "/enquiry", "/admissions.html")),
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)
TEL_RE = re.compile(r"tel:([+0-9()\-.\s]{7,})", re.I)
# A phone number needs either a + country code or enough digits to be one.
PHONE_CAND_RE = re.compile(r"(?:\+\d{1,3}[\s\-.]?)?(?:\(?\d{2,5}\)?[\s\-.]?){1,3}\d{3,5}")

# An address ending in an asset extension came out of a filename, not a
# footer: "logo@2x.png", "sprite@main.svg".
ASSET_TAIL_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|bmp|tiff?|css|js|json|woff2?|ttf|eot|"
    r"pdf|docx?|xlsx?|mp4|webm|zip)$", re.I)

# Domains that are never the business: placeholders in boilerplate, and the
# telemetry/CDN vendors whose keys happen to be shaped like addresses.
JUNK_EMAIL_DOMAINS = re.compile(
    r"(^|\.)("
    r"example\.(com|org|net)|example|domain\.com|yourdomain\.[a-z]+|"
    r"yourcompany\.[a-z]+|email\.com|mail\.com|test\.com|"
    r"sentry\.io|ingest\.sentry\.io|wixpress\.com|wix\.com|parastorage\.com|"
    r"schema\.org|w3\.org|sentry-cdn\.com|googleapis\.com|gstatic\.com|"
    r"cloudflare\.com|jquery\.com|bootstrapcdn\.com|fontawesome\.com"
    r")$", re.I)

# Consumer mailboxes. A great many schools in India and the Gulf publish a
# gmail or rediffmail address as their real contact point, so these are KEPT
# — but flagged, because they carry no domain pattern, cannot be verified by
# domain, and say nothing about the school's own mail setup.
FREE_MAIL_DOMAINS = frozenset("""
gmail.com googlemail.com yahoo.com yahoo.co.in yahoo.co.uk ymail.com
rediffmail.com rediff.com outlook.com hotmail.com live.com msn.com
aol.com icloud.com me.com mac.com protonmail.com proton.me zoho.com
zohomail.in mail.ru yandex.com gmx.com inbox.com sify.com vsnl.net
vsnl.com bsnl.in airtelmail.in indiatimes.com emirates.net.ae eim.ae
""".split())


def is_free_mail(email: str) -> bool:
    """True when the address sits on a consumer mail provider."""
    return email.partition("@")[2].lower() in FREE_MAIL_DOMAINS


# Local parts that are machinery, not a mailbox anyone reads.
JUNK_LOCAL_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|bounce|mailer-daemon|postmaster|"
    r"abuse|null|void|your-?email|email|user|username|name|sentry|"
    r"[0-9a-f]{16,})$", re.I)

# A mailbox the school publishes for "whoever is on duty" — real and worth
# contacting, but not a named person.
GENERIC_LOCALS = frozenset("""
info office admin administration administrator contact contacts enquiry
enquiries inquiry inquiries principal school mail hello help support
reception frontdesk front-desk hr careers career admission admissions
accounts account fees finance webmaster feedback general query queries
communication office365 students parents alumni library transport
""".split())

# ── Which published address actually belongs to this school ──────────
#
# A school's contact page routinely lists addresses that are not the
# school's: sibling campuses in the same group, and (for school groups
# operating abroad) campuses in other countries. Storing those against this
# school is wrong — the row would name the right group and the wrong place.
# So every address is classified against the school's own domain.

# Recruitment mailboxes. Real, worth keeping, never the address you open a
# sales conversation on.
NEVER_PRIMARY = frozenset("""
hr careers career jobs job recruitment recruit vacancy vacancies hiring
placement placements resume cv
""".split())

# Primary preference: the person first, then the front desk, then admissions.
PRIMARY_ORDER = (
    ("principal", 10), ("headmaster", 10), ("headmistress", 10),
    ("director", 12), ("correspondent", 12), ("chairman", 14),
    ("office", 20), ("info", 21), ("contact", 22), ("reception", 23),
    ("enquiry", 24), ("enquiries", 24), ("inquiry", 24), ("mail", 25),
    ("school", 26), ("admin", 27), ("administration", 27),
    ("admission", 30), ("admissions", 30),
)

# Place tokens that mean "a different country's campus". Only non-target
# places belong here: India and the UAE are the market, so their city names
# must never appear.
FOREIGN_PLACE_TOKENS = frozenset("""
palembang bandung cilegon semarang jakarta medan surabaya bali denpasar
bogor bekasi tangerang yogyakarta makassar indonesia
singapore malaysia kualalumpur penang johor
bangkok thailand phuket chiangmai
hanoi saigon hochiminh vietnam manila philippines cambodia phnompenh
yangon myanmar colombo srilanka lanka kathmandu nepal dhaka bangladesh
karachi lahore pakistan kabul afghanistan
nairobi kenya lagos nigeria accra ghana kampala uganda tanzania
addis ethiopia cairo egypt morocco casablanca
london uk britain paris france berlin germany madrid spain lisbon
tokyo japan korea seoul china shanghai beijing hongkong
sydney australia toronto canada newyork usa
""".split())


def registrable(domain: str) -> str:
    """Rough registrable domain: enough to tell two organisations apart."""
    parts = (domain or "").lower().strip(".").split(".")
    if len(parts) < 2:
        return domain or ""
    if len(parts) > 2 and parts[-2] in (
            "co", "edu", "ac", "net", "org", "gov", "sch", "com"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def brand_token(domain: str) -> str:
    """The distinctive label of a domain, for spotting sibling campuses."""
    first = registrable(domain).split(".")[0]
    return re.sub(r"[^a-z0-9]", "", first.lower())


def looks_foreign_campus(email: str, school_domain: str) -> bool:
    """True when the address names a campus in another country."""
    local = email.partition("@")[0].lower()
    dom = email.partition("@")[2].lower()
    hay = re.sub(r"[^a-z]", " ", local + " " + dom.replace(
        registrable(school_domain), " "))
    tokens = set(hay.split())
    if tokens & FOREIGN_PLACE_TOKENS:
        return True
    # A compacted form ("sispalembang") hides the token from a word split.
    return any(t in local for t in FOREIGN_PLACE_TOKENS if len(t) > 5)


def classify_email(email: str, school_domain: str) -> str:
    """One of: own | freemail | group | foreign | unrelated."""
    dom = email.partition("@")[2].lower()
    if is_free_mail(email):
        return "freemail"
    if registrable(dom) == registrable(school_domain):
        return "own"
    if looks_foreign_campus(email, school_domain):
        return "foreign"
    bt, st = brand_token(dom), brand_token(school_domain)
    if bt and st and (bt == st or (len(bt) >= 4 and len(st) >= 4
                                   and (bt in st or st in bt))):
        return "group"
    return "unrelated"


# Sendable classes, and the score each gets. "group" is deliberately kept
# but scored to the floor: useful for a multi-campus conversation, never a
# cold-send target.
CLASS_STATUS = {"own": "published", "freemail": "published_freemail",
                "group": "group_related"}
CLASS_SCORE = {"own": 60, "freemail": 50, "group": 10}


def primary_rank(email: str) -> int:
    """Lower sorts first. Recruitment mailboxes can never win."""
    local = email.partition("@")[0].lower()
    stem = re.split(r"[._\-+0-9]+", local)[0] or local
    if local in NEVER_PRIMARY or stem in NEVER_PRIMARY:
        return 999
    for needle, rank in PRIMARY_ORDER:
        if local == needle or stem == needle:
            return rank
    return 40          # a named person's address, below the role mailboxes


def choose_primary(emails: list[tuple[str, str]]) -> str:
    """Pick one address per school. emails = [(email, class), ...]."""
    ranked = sorted(
        ((primary_rank(e), 0 if c == "own" else 1, len(e), e)
         for e, c in emails if c in ("own", "freemail")),
    )
    return ranked[0][3] if ranked and ranked[0][0] < 999 else ""


# Titles good enough to show in a list without inventing seniority.
GENERIC_TITLES = {
    "admission": "Admissions", "admissions": "Admissions",
    "principal": "Principal", "accounts": "Accounts", "fees": "Accounts",
    "finance": "Accounts", "hr": "HR", "careers": "HR", "career": "HR",
}

UA = ("Mozilla/5.0 (compatible; Harvey/0.3; +https://github.com/"
      "ethanplusai/harvey) contact-extractor")


def clean_email(raw: str) -> str:
    """Normalise one candidate, or return "" when it isn't an address."""
    e = (raw or "").strip().strip(".,;:<>()[]\"'").lower()
    e = e.split("?")[0]                      # mailto:a@b?subject=...
    if e.count("@") != 1 or ".." in e:
        return ""
    local, _, domain = e.partition("@")
    if not local or not domain or "." not in domain:
        return ""
    if ASSET_TAIL_RE.search(domain) or ASSET_TAIL_RE.search(e):
        return ""
    if JUNK_EMAIL_DOMAINS.search(domain) or JUNK_LOCAL_RE.match(local):
        return ""
    if len(e) > 100 or domain.endswith("."):
        return ""
    return e


def clean_phone(raw: str) -> str:
    """Digits-and-plus form of a plausible phone number, else ""."""
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if not 9 <= len(digits) <= 15:
        return ""
    # A run of identical or sequential digits is a placeholder, not a number.
    if len(set(digits)) <= 2 or digits in "01234567890123456789":
        return ""
    return ("+" + digits) if s.lstrip().startswith("+") else digits


def is_generic(email: str) -> bool:
    """True when the address is a role mailbox rather than a person."""
    local = email.partition("@")[0]
    stem = re.split(r"[._\-+0-9]+", local)[0] or local
    return local in GENERIC_LOCALS or stem in GENERIC_LOCALS


def title_for(email: str) -> str:
    local = email.partition("@")[0]
    stem = re.split(r"[._\-+0-9]+", local)[0] or local
    return GENERIC_TITLES.get(stem, "School Office")


def split_person(label: str) -> tuple[str, str, str]:
    """Split a CONTACT_FOUND observation ("Jane Rao — Principal")."""
    text = (label or "").replace("—", "-")
    name, _, title = text.partition(" - ")
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "", "", title.strip()
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""
    return first, last, title.strip()


def match_person(email: str, people: list[tuple[str, str, str]]
                 ) -> tuple[str, str, str] | None:
    """Find a profiled person whose name appears in the address.

    This is matching, not guessing: the address is already published, and
    all we decide is whose it is. A single-token local part must equal a
    first or last name outright; a dotted one must contain both.
    """
    local = email.partition("@")[0].lower()
    tokens = [t for t in re.split(r"[._\-+0-9]+", local) if t]
    for first, last, title in people:
        f, l = first.lower(), last.lower()
        if f and l and ((f in tokens and l in tokens)
                        or local in (f + l, l + f, f + "." + l, l + "." + f)):
            return first, last, title
    if len(tokens) == 1:
        for first, last, title in people:
            if tokens[0] in (first.lower(), last.lower()) and len(tokens[0]) > 3:
                return first, last, title
    return None


class ContactExtractor:
    """Reads each company's own pages and stores the addresses it finds."""

    def __init__(self, state, config, concurrency: int = 4,
                 timeout: float = 12.0, max_bytes: int = 600_000,
                 insecure: bool = False):
        self.state = state
        self.config = config
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        # Opt-in: accept expired/self-signed certs. Only defensible because
        # every page read here is public and nothing is ever sent to it — a
        # tampered page could at worst plant a wrong address, which the
        # stored evidence URL makes auditable.
        self.insecure = insecure
        # School sites embed enormous galleries; an address in a footer is in
        # the first few hundred KB or it isn't worth the memory.
        self.max_bytes = max_bytes

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            r = await client.get(url, timeout=self.timeout,
                                 follow_redirects=True,
                                 headers={"User-Agent": UA})
        except Exception as e:
            logger.debug("contacts: GET %s failed: %s", url, e)
            return ""
        if r.status_code != 200:
            return ""
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return ""
        return r.text[:self.max_bytes]

    async def _pages(self, client: httpx.AsyncClient, domain: str
                     ) -> list[tuple[str, str]]:
        """(url, html) for the homepage and the first hit in each group."""
        base = f"https://{domain}"
        out: list[tuple[str, str]] = []
        for _group, paths in PAGE_GROUPS:
            for path in paths:
                url = urljoin(base + "/", path.lstrip("/")) if path else base
                html = await self._get(client, url)
                if html:
                    out.append((url, html))
                    break
        return out

    @staticmethod
    def _harvest(html: str) -> tuple[set[str], set[str]]:
        """Every address and phone number on one page."""
        emails, phones = set(), set()
        for raw in MAILTO_RE.findall(html):
            if (e := clean_email(raw)):
                emails.add(e)
        for raw in EMAIL_RE.findall(html):
            if (e := clean_email(raw)):
                emails.add(e)
        for raw in TEL_RE.findall(html):
            if (p := clean_phone(raw)):
                phones.add(p)
        return emails, phones

    async def _people_for(self, company_id: str
                          ) -> list[tuple[str, str, str]]:
        """Names profiling already found at this company."""
        # Profiling writes DECISION_MAKER_TITLE only for people it also wrote
        # CONTACT_FOUND for, so one query covers everyone it found.
        try:
            obs = await self.state.get_observations(
                company_id=company_id, signal_code="CONTACT_FOUND", limit=50)
        except Exception as e:
            logger.debug("contacts: no profiled names for %s: %s",
                         company_id, e)
            return []
        people, seen = [], set()
        for o in obs or []:
            first, last, title = split_person(o.get("value_text") or "")
            if first and (key := (first.lower(), last.lower())) not in seen:
                seen.add(key)
                people.append((first, last, title))
        return people

    async def run_one(self, client: httpx.AsyncClient, company: dict) -> dict:
        """Extract and store contacts for one company."""
        domain = (company.get("domain") or "").strip()
        result = {"domain": domain, "emails": 0, "named": 0, "generic": 0,
                  "phones": 0, "added": 0, "free_mail": 0,
                  "group_related": 0, "primary": 0,
                  "dropped_foreign": 0, "dropped_unrelated": 0}
        if not domain:
            return result

        emails: dict[str, str] = {}      # email -> evidence url
        phones: dict[str, str] = {}
        for url, html in await self._pages(client, domain):
            page_emails, page_phones = self._harvest(html)
            for e in page_emails:
                emails.setdefault(e, url)
            for p in page_phones:
                phones.setdefault(p, url)

        result["phones"] = len(phones)
        if not emails:
            return result

        # Classify against the school's own domain, drop what isn't theirs.
        classed: dict[str, str] = {}
        for email in list(emails):
            cls = classify_email(email, domain)
            if cls == "foreign":
                result["dropped_foreign"] += 1
                emails.pop(email, None)
            elif cls == "unrelated":
                result["dropped_unrelated"] += 1
                emails.pop(email, None)
            else:
                classed[email] = cls

        result["emails"] = len(emails)
        if not emails:
            return result

        primary = choose_primary(list(classed.items()))
        people = await self._people_for(company["id"])
        fallback_phone = (company.get("phone") or "").strip() or (
            next(iter(phones), "") if phones else "")

        for email, evidence in sorted(emails.items()):
            person = match_person(email, people)
            if person and not is_generic(email):
                first, last, title = person
                result["named"] += 1
            elif is_generic(email):
                first, last, title = "", "", title_for(email)
                result["generic"] += 1
            else:
                # A non-generic local part with no profiled name behind it.
                # Record it as unnamed rather than inventing a person.
                first, last, title = "", "", "School Office"
                result["generic"] += 1

            cls = classed[email]
            is_primary = (email == primary)
            if cls == "freemail":
                result["free_mail"] += 1
            if cls == "group":
                result["group_related"] += 1
            if is_primary:
                result["primary"] += 1

            note = f"Address published on {evidence}"
            if cls == "freemail":
                note += (f" — free-mail ({email.partition('@')[2]}), "
                         "no domain pattern and not domain-verifiable")
            elif cls == "group":
                note += (" — belongs to a related school/group domain, not "
                         "this campus. Not a cold-send target.")
            if is_primary:
                note += " [PRIMARY]"

            prospect = Prospect(
                company_id=company["id"],
                first_name=first, last_name=last, title=title,
                email=email,
                email_verified=False,
                # None of these are sendable statuses: the pre-send gate
                # passes only verified/risky.
                email_status=CLASS_STATUS[cls],
                # 100 marks the one address to open on; the rest sit below.
                score=100 if is_primary else CLASS_SCORE[cls],
                phone=fallback_phone,
                source=("found on website (group/related)" if cls == "group"
                        else "found on website (free-mail)" if cls == "freemail"
                        else "found on website"),
                source_url=evidence,
                company=company.get("name") or domain,
                industry=company.get("industry") or "",
                personalization_notes=note,
            )
            try:
                await self.state.add_prospect(prospect)
                result["added"] += 1
            except Exception as e:
                logger.warning("contacts: could not store %s: %s", email, e)
        return result

    async def run(self, companies: list[dict], batch_size: int = 25) -> dict:
        """Process companies in small batches to keep memory flat."""
        totals = {"companies": 0, "with_email": 0, "emails": 0, "named": 0,
                  "generic": 0, "phone_only": 0, "neither": 0, "added": 0,
                  "free_mail": 0, "group_related": 0,
                  "primary": 0, "dropped_foreign": 0,
                  "dropped_unrelated": 0}
        sem = asyncio.Semaphore(self.concurrency)

        async def one(client, c):
            async with sem:
                return await self.run_one(client, c)

        limits = httpx.Limits(max_connections=self.concurrency,
                              max_keepalive_connections=2)
        for start in range(0, len(companies), batch_size):
            batch = companies[start:start + batch_size]
            async with httpx.AsyncClient(limits=limits,
                                         verify=not self.insecure) as client:
                results = await asyncio.gather(
                    *(one(client, c) for c in batch), return_exceptions=True)
            for c, r in zip(batch, results):
                totals["companies"] += 1
                if isinstance(r, BaseException):
                    logger.warning("contacts: %s raised: %s",
                                   c.get("domain"), r)
                    totals["neither"] += 1
                    continue
                totals["emails"] += r["emails"]
                totals["named"] += r["named"]
                totals["generic"] += r["generic"]
                totals["added"] += r["added"]
                for k in ("free_mail", "group_related", "primary",
                          "dropped_foreign", "dropped_unrelated"):
                    totals[k] += r[k]
                if r["emails"]:
                    totals["with_email"] += 1
                elif r["phones"] or (c.get("phone") or "").strip():
                    totals["phone_only"] += 1
                else:
                    totals["neither"] += 1
            logger.info("contacts: %d/%d companies, %d emails so far",
                        totals["companies"], len(companies), totals["emails"])
        return totals
