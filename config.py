"""
config.py — Configuration for the Burnham Tracker.

A read-only intelligence dashboard tracking what Andy Burnham (and his inner
circle) are saying as he moves toward No. 10. See SPEC.md for the full design.
"""

# ---------------------------------------------------------------------------
# Fetch tuning
# ---------------------------------------------------------------------------

MAX_ARTICLES_PER_FEED = 40

# Lookback windows per feed category. The relevant period is the leadership
# campaign and its run-up, so we keep a generous window.
LOOKBACK_HOURS_BY_CATEGORY = {
    "media":      720,   # 30 days
    "social":     720,   # 30 days — his own X output
    "other":      720,   # 30 days
}

# ---------------------------------------------------------------------------
# Who we track
# ---------------------------------------------------------------------------

BURNHAM_HANDLE = "AndyBurnhamGM"   # his X handle (still active post-mayoralty)

# The tracker's timeline begins when the Makerfield result came in (19 Jun 2026)
# — his entry to Parliament and the start of the leadership run. The heatmap
# spans this date through today.
TIMELINE_START_DATE = "2026-06-19"

# Name keywords used to pre-filter news articles down to ones that plausibly
# carry his words before we spend a Claude call on them.
BURNHAM_NAME_KEYWORDS = ["burnham"]

# Inner circle — captured only when they say something substantive, and always
# attributed to the named adviser, never to Burnham. Roles confirmed at build.
INNER_CIRCLE = {
    "James Purnell":     "ally / former Cabinet minister",
    "Kevin Lee":         "chief of staff",
    "Kate Green":        "ally / former MP",
    "Caroline Simpson":  "senior adviser",
    "Grace Pritchard":   "communications",
}

# ---------------------------------------------------------------------------
# Policy areas (PM scope, light Burnham specificity). Order = heatmap row order.
# ---------------------------------------------------------------------------

POLICY_AREAS = [
    "Economy, Tax & Spending",
    "Health & Social Care",
    "Housing & Homelessness",
    "Devolution & Local Government",
    "Transport & Infrastructure",
    "Energy & Net Zero",
    "Work, Pay & Welfare",
    "Business, Industrial Strategy & Trade",
    "Crime, Policing & Justice",
    "Immigration & Asylum",
    "Education & Skills",
    "Foreign Affairs & Defence",
]

POLICY_AREA_DESCRIPTIONS = {
    "Economy, Tax & Spending": (
        "HM Treasury, growth strategy, fiscal rules, taxation, public spending "
        "and the Budget, borrowing, the OBR, wages and the cost of living."
    ),
    "Health & Social Care": (
        "NHS funding, waiting lists, reform and staffing; adult social care "
        "reform and funding (a Burnham signature — e.g. a National Care "
        "Service, free personal care); public health and mental health."
    ),
    "Housing & Homelessness": (
        "Housebuilding and supply, planning reform, affordable and social "
        "housing, the private rented sector and renters' rights, rough "
        "sleeping and homelessness, leasehold."
    ),
    "Devolution & Local Government": (
        "English devolution and giving power to regions/mayors (a Burnham "
        "signature), local government funding, the North, council finances, "
        "Whitehall reform and the constitution."
    ),
    "Transport & Infrastructure": (
        "Rail, buses and bus franchising, roads, aviation, ports, active "
        "travel, major infrastructure delivery, HS2 and Northern transport."
    ),
    "Energy & Net Zero": (
        "Electricity and the grid, renewables, nuclear, energy bills and "
        "security, net zero targets, green industry and industrial "
        "decarbonisation."
    ),
    "Work, Pay & Welfare": (
        "Workers' rights and employment law, the minimum/living wage, trade "
        "unions, benefits and Universal Credit, pensions, child poverty."
    ),
    "Business, Industrial Strategy & Trade": (
        "Industrial strategy, manufacturing and key sectors, business "
        "regulation and taxation, investment, trade policy and exports."
    ),
    "Crime, Policing & Justice": (
        "Policing and crime, the courts and prisons, justice reform, victims, "
        "and accountability measures such as a Hillsborough Law / duty of "
        "candour (a Burnham signature)."
    ),
    "Immigration & Asylum": (
        "Legal and illegal migration, the asylum system and small boats, "
        "borders, settlement and integration."
    ),
    "Education & Skills": (
        "Schools, early years, further and higher education, apprenticeships "
        "and technical/vocational skills, tuition fees."
    ),
    "Foreign Affairs & Defence": (
        "Defence spending and the armed forces, NATO, Ukraine and the Middle "
        "East, the EU relationship, international development and diplomacy."
    ),
}

# ---------------------------------------------------------------------------
# Solidity — a property of an individual position, ordered weakest → strongest.
# This is what the per-area timeline plots on its y-axis.
# ---------------------------------------------------------------------------

SOLIDITY_LEVELS = ["topic", "emerging", "firm"]

SOLIDITY_DESCRIPTIONS = {
    "topic": (
        "A subject or theme he's raised — including slogans and vision lines "
        "('a rewired Britain', 'end trickle-down economics') — with no specific "
        "policy attached."
    ),
    "emerging": (
        "A clear direction or intention, but without the specifics — you can "
        "tell what he wants, not yet exactly what he would do "
        "('wants more public control of utilities')."
    ),
    "firm": (
        "A specific, concrete policy commitment — a clear action he says he "
        "will take, with the actual substance of what (and often how or when): "
        "e.g. 'open a No.10 North government hub in Manchester', 'put the whole "
        "£39bn affordable-homes budget into social rent'."
    ),
}

SOLIDITY_RANK = {level: i for i, level in enumerate(SOLIDITY_LEVELS)}

# ---------------------------------------------------------------------------
# Source colours (left border / badges in the UI)
# ---------------------------------------------------------------------------

SOURCE_COLORS = {
    "Andy Burnham (X)":                 "#1d9bf0",
    "Guardian Politics":                "#052962",
    "BBC Politics":                     "#bb1919",
    "The Independent — Politics":       "#ee2c30",
    "Sky News — Politics":              "#0c4da2",
    "New Statesman":                    "#0a0a0a",
    "LabourList":                       "#e4003b",
    "PoliticsHome":                     "#1a3a6b",
    "Parliament — Written Questions":   "#6c3483",
    "Parliament — Written Statements":  "#6c3483",
    "Parliament — Bills":               "#6c3483",
    "Inner circle":                     "#b8860b",
}
DEFAULT_COLOR = "#555555"

# ---------------------------------------------------------------------------
# News feeds — direct publisher feeds with REAL article URLs (so we can fetch
# the full body and lift his actual quotes). Google News was dropped: its RSS
# links bounce through a consent/redirect page and can't be fetched. Every item
# is keyword pre-filtered for Burnham, then its article body is fetched before
# the AI sees it. Paywalled outlets (FT, Times, Telegraph) are intentionally
# omitted — the fetcher can't read them.
# ---------------------------------------------------------------------------

NEWS_FEEDS = {
    "Guardian Politics": {
        "url":      "https://www.theguardian.com/politics/rss",
        "category": "media",
    },
    "BBC Politics": {
        "url":      "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "category": "media",
    },
    "The Independent — Politics": {
        "url":      "https://www.independent.co.uk/news/uk/politics/rss",
        "category": "media",
    },
    "Sky News — Politics": {
        "url":      "https://feeds.skynews.com/feeds/rss/politics.xml",
        "category": "media",
    },
    "New Statesman": {
        "url":      "https://www.newstatesman.com/feed",
        "category": "media",
    },
    "LabourList": {
        "url":      "https://labourlist.org/feed/",
        "category": "media",
    },
    "PoliticsHome": {
        "url":      "https://www.politicshome.com/news/rss",
        "category": "media",
    },
}

# Domains the article fetcher should not bother with (hard paywalls).
PAYWALLED_DOMAINS = [
    "ft.com", "thetimes.co.uk", "telegraph.co.uk", "economist.com",
    "thetimes.com", "wsj.com", "bloomberg.com",
]

# ---------------------------------------------------------------------------
# Twitter / X via RSSHub  (public instances tried in order; graceful fallback)
# ---------------------------------------------------------------------------

RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rss.shab.fun",
]

# ---------------------------------------------------------------------------
# Parliament — now he is an MP, anything mentioning him is relevant.
# Committees disabled (low signal for a single individual); the written
# questions / statements APIs are keyword-filtered below.
# ---------------------------------------------------------------------------

PARLIAMENT_COMMITTEES: list[dict] = []

PARLIAMENT_KEYWORD_FILTERS = [
    "burnham",
]
