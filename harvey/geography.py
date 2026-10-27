"""Turn a source's free-text location label into country / state / city.

Every collector records ``companies.location`` in whatever shape its source
gave it:

    "Indiranagar, Bengaluru, India"   locality discovery
    "BANGALORE URBAN, KARNATAKA"      CBSE directory, Indian school
    "BAHRAIN"                         CBSE directory, overseas school
    "Porur, Chennai"                  older discovery rows

Which is fine as provenance and useless as a filter. This parses those into
discrete fields once, so the dashboard can group and filter on them instead
of running LIKE queries over a string whose format depends on where the row
came from. ``location`` itself is never rewritten.
"""

import re

# Indian states and union territories, as SARAS and common usage spell them.
# Mapped to a canonical display form.
INDIAN_STATES = {
    "ANDAMAN & NICOBAR": "Andaman & Nicobar", "ANDHRA PRADESH": "Andhra Pradesh",
    "ARUNACHAL PRADESH": "Arunachal Pradesh", "ASSAM": "Assam", "BIHAR": "Bihar",
    "CHANDIGARH": "Chandigarh", "CHATTISGARH": "Chhattisgarh",
    "CHHATTISGARH": "Chhattisgarh", "DADAR & NAGAR HAVELI": "Dadra & Nagar Haveli",
    "DAMAN & DIU": "Daman & Diu", "DELHI": "Delhi", "GOA": "Goa",
    "GUJARAT": "Gujarat", "HARYANA": "Haryana", "HIMACHAL PRADESH": "Himachal Pradesh",
    "JAMMU & KASHMIR": "Jammu & Kashmir", "JHARKHAND": "Jharkhand",
    "KARNATAKA": "Karnataka", "KERALA": "Kerala", "LADAKH": "Ladakh",
    "LAKSHADWEEP": "Lakshadweep", "MADHYA PRADESH": "Madhya Pradesh",
    "MAHARASHTRA": "Maharashtra", "MANIPUR": "Manipur", "MEGHALAYA": "Meghalaya",
    "MIZORAM": "Mizoram", "NAGALAND": "Nagaland", "ODISHA": "Odisha",
    "PUDUCHERRY": "Puducherry", "PUNJAB": "Punjab", "RAJASTHAN": "Rajasthan",
    "SIKKIM": "Sikkim", "TAMILNADU": "Tamil Nadu", "TAMIL NADU": "Tamil Nadu",
    "TELANGANA": "Telangana", "TRIPURA": "Tripura",
    "UTTAR PRADESH": "Uttar Pradesh", "UTTARAKHAND": "Uttarakhand",
    "WEST BENGAL": "West Bengal",
}

# Countries a label may name directly. CBSE's overseas list puts the country
# in the district column, so these arrive with no other context.
COUNTRIES = {
    "INDIA": "India", "UAE": "UAE", "UNITED ARAB EMIRATES": "UAE",
    "BAHRAIN": "Bahrain", "BANGLADESH": "Bangladesh", "QATAR": "Qatar",
    "KUWAIT": "Kuwait", "OMAN": "Oman", "SAUDI ARABIA": "Saudi Arabia",
    "KSA": "Saudi Arabia", "NEPAL": "Nepal", "SRI LANKA": "Sri Lanka",
    "MALAYSIA": "Malaysia", "SINGAPORE": "Singapore", "INDONESIA": "Indonesia",
    "THAILAND": "Thailand", "JAPAN": "Japan", "MYANMAR": "Myanmar",
    "IRAN": "Iran", "KENYA": "Kenya", "NIGERIA": "Nigeria", "GHANA": "Ghana",
    "ETHIOPIA": "Ethiopia", "ANGOLA": "Angola", "LIBYA": "Libya",
    "TANZANIA": "Tanzania", "UGANDA": "Uganda", "ZAMBIA": "Zambia",
    "ZIMBABWE": "Zimbabwe", "EGYPT": "Egypt", "MOROCCO": "Morocco",
    "REPUBLIC OF BENIN": "Benin", "BENIN": "Benin", "RUSSIA": "Russia",
    "UK": "United Kingdom", "UNITED KINGDOM": "United Kingdom",
    "USA": "United States", "UNITED STATES": "United States",
    "VIETNAM": "Vietnam", "PHILIPPINES": "Philippines", "MAURITIUS": "Mauritius",
    "TURKMENISTAN": "Turkmenistan", "UZBEKISTAN": "Uzbekistan",
    "AFGHANISTAN": "Afghanistan", "BHUTAN": "Bhutan", "MALDIVES": "Maldives",
}

# UAE emirates, so a Gulf city resolves to a country rather than nothing.
UAE_CITIES = {"DUBAI", "SHARJAH", "ABU DHABI", "AJMAN", "FUJAIRAH",
              "RAS AL KHAIMAH", "UMM AL QUWAIN", "AL AIN"}

# Indian cities and districts whose state the label itself never gives.
# Discovery writes "Porur, Chennai" and the directory writes
# "BANGALORE URBAN, KARNATAKA" — without this, filtering by Karnataka
# would silently miss every school discovery found.
CITY_TO_STATE = {
    "BENGALURU": "Karnataka", "BANGALORE": "Karnataka",
    "BANGALORE URBAN": "Karnataka", "BANGALORE RURAL": "Karnataka",
    "MYSURU": "Karnataka", "MYSORE": "Karnataka", "MANGALURU": "Karnataka",
    "MANGALORE": "Karnataka", "HUBBALLI": "Karnataka", "BELAGAVI": "Karnataka",
    "HYDERABAD": "Telangana", "SECUNDERABAD": "Telangana",
    "RANGA REDDY": "Telangana", "RANGAREDDY": "Telangana",
    "MEDCHAL": "Telangana", "WARANGAL": "Telangana",
    "CHENNAI": "Tamil Nadu", "COIMBATORE": "Tamil Nadu",
    "MADURAI": "Tamil Nadu", "TIRUCHIRAPPALLI": "Tamil Nadu",
    "SALEM": "Tamil Nadu", "KANCHIPURAM": "Tamil Nadu",
    "TIRUVALLUR": "Tamil Nadu", "ERODE": "Tamil Nadu",
    "MUMBAI": "Maharashtra", "NAVI MUMBAI": "Maharashtra",
    "THANE": "Maharashtra", "PUNE": "Maharashtra", "NAGPUR": "Maharashtra",
    "NASHIK": "Maharashtra", "AURANGABAD": "Maharashtra",
    "NEW DELHI": "Delhi", "DELHI": "Delhi", "SOUTH WEST DELHI": "Delhi",
    "NORTH WEST DELHI": "Delhi", "EAST DELHI": "Delhi",
    "GURUGRAM": "Haryana", "GURGAON": "Haryana", "FARIDABAD": "Haryana",
    "NOIDA": "Uttar Pradesh", "GHAZIABAD": "Uttar Pradesh",
    "GAUTAM BUDDHA NAGAR": "Uttar Pradesh", "LUCKNOW": "Uttar Pradesh",
    "KOLKATA": "West Bengal", "HOWRAH": "West Bengal",
    "AHMEDABAD": "Gujarat", "SURAT": "Gujarat", "VADODARA": "Gujarat",
    "RAJKOT": "Gujarat", "GANDHINAGAR": "Gujarat",
    "KOCHI": "Kerala", "ERNAKULAM": "Kerala", "THIRUVANANTHAPURAM": "Kerala",
    "KOZHIKODE": "Kerala", "THRISSUR": "Kerala",
    "JAIPUR": "Rajasthan", "JODHPUR": "Rajasthan", "KOTA": "Rajasthan",
    "INDORE": "Madhya Pradesh", "BHOPAL": "Madhya Pradesh",
    "VISAKHAPATNAM": "Andhra Pradesh", "VIJAYAWADA": "Andhra Pradesh",
    "GUNTUR": "Andhra Pradesh", "CHANDIGARH": "Chandigarh",
    "LUDHIANA": "Punjab", "AMRITSAR": "Punjab", "MOHALI": "Punjab",
    "PATNA": "Bihar", "RANCHI": "Jharkhand", "BHUBANESWAR": "Odisha",
    "DEHRADUN": "Uttarakhand", "GUWAHATI": "Assam", "RAIPUR": "Chhattisgarh",
}

BOARD_PATTERNS = (
    ("CBSE", re.compile(r"\bcbse\b", re.I)),
    ("ICSE/ISC", re.compile(r"\b(icse|isc|cisce)\b", re.I)),
    ("IB", re.compile(r"\b(ib|international baccalaureate)\b", re.I)),
    ("IGCSE/Cambridge", re.compile(r"\b(igcse|cambridge|caie)\b", re.I)),
    ("State board", re.compile(r"\bstate board\b", re.I)),
)


def _canon(part: str) -> str:
    return re.sub(r"\s+", " ", (part or "")).strip(" ,.").strip()


def _display(part: str) -> str:
    """Tidy a SHOUTED directory value for a filter dropdown.

    Only when the whole thing is uppercase — a value a human already
    cased, or one with deliberate capitals, is left alone.
    """
    part = _canon(part)
    if part and part == part.upper() and re.search(r"[A-Z]{2}", part):
        return re.sub(r"\b([A-Z])([A-Z']*)\b",
                      lambda m: m.group(1) + m.group(2).lower(), part)
    return part


def parse_location(location: str, source: str = "") -> tuple[str, str, str]:
    """(country, state_region, city) from a location label.

    Unknown pieces come back empty rather than guessed — a blank filter
    option is honest, a wrong country is not.
    """
    parts = [_canon(p) for p in (location or "").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return "", "", ""

    upper = [p.upper() for p in parts]
    country = state = city = ""

    # Last part names a country outright: "…, India", or a bare "BAHRAIN".
    if upper[-1] in COUNTRIES:
        country = COUNTRIES[upper[-1]]
        rest = parts[:-1]
    # Last part is an Indian state: the directory shape "DISTRICT, STATE".
    elif upper[-1] in INDIAN_STATES:
        country, state = "India", INDIAN_STATES[upper[-1]]
        rest = parts[:-1]
    else:
        rest = parts

    # A Gulf city tells us the country even when nothing else does.
    for i, u in enumerate(upper):
        if u in UAE_CITIES:
            country = country or "UAE"
            city = city or parts[i]
            break

    if not city and rest:
        # "Indiranagar, Bengaluru" -> city is the broader, trailing part.
        # "BANGALORE URBAN" alone -> that is the city/district.
        city = rest[-1]
    # A lone Indian state label should not masquerade as a city — unless it
    # is also a city in its own right ("CHANDIGARH, CHANDIGARH").
    if city.upper() in INDIAN_STATES and city.upper() not in CITY_TO_STATE:
        state = state or INDIAN_STATES[city.upper()]
        city = ""
    if city.upper() in COUNTRIES:
        country = country or COUNTRIES[city.upper()]
        city = ""
    # A known Indian city fills in the state the label never named, so one
    # "Karnataka" filter reaches directory and discovery rows alike.
    known = CITY_TO_STATE.get(city.upper())
    if known:
        state = state or known
        country = country or "India"
    return country, state, _display(city)


def parse_board(source: str = "", industry: str = "", name: str = "") -> str:
    """Which examination board, where the row says so."""
    if (source or "").strip() == "CBSE directory":
        return "CBSE"
    hay = f"{industry} {name}"
    for label, pattern in BOARD_PATTERNS:
        if pattern.search(hay):
            return label
    return ""


async def backfill_geography(state, only_blank: bool = True) -> dict:
    """Fill country/state_region/city/board on companies.

    Idempotent, and by default touches only rows where the field is still
    blank, so a hand-corrected value is never overwritten. Nothing is
    deleted and ``location`` is left exactly as the collector wrote it.
    """
    import aiosqlite

    updated = {"rows": 0, "country": 0, "state": 0, "city": 0, "board": 0}
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        where = ("WHERE country = '' OR state_region = '' OR city = '' "
                 "OR board = ''") if only_blank else ""
        async with db.execute(
                f"SELECT id, location, source, industry, name, country, "
                f"state_region, city, board FROM companies {where}") as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        for r in rows:
            country, region, city = parse_location(r["location"], r["source"])
            board = parse_board(r["source"], r["industry"], r["name"])
            sets, params = [], []
            for col, new, key in (("country", country, "country"),
                                  ("state_region", region, "state"),
                                  ("city", city, "city"),
                                  ("board", board, "board")):
                if new and (not only_blank or not (r[col] or "").strip()):
                    sets.append(f"{col} = ?")
                    params.append(new)
                    updated[key] += 1
            if sets:
                params.append(r["id"])
                await db.execute(
                    f"UPDATE companies SET {', '.join(sets)} WHERE id = ?",
                    params)
                updated["rows"] += 1
        await db.commit()
    return updated
