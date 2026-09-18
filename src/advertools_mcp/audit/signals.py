"""Per-check data-signal requirements, for auditing imported (non-advertools) datasets.

When the audit runs over an advertools crawl the full schema is present, so no
gating applies (``available_signals`` is ``None``). When it runs over an
imported dataset (e.g. a Screaming Frog export) only some signals are present;
a check whose required signal is missing must report **Not assessed**, never a
false Present/Not present from empty columns.

Each entry maps a check number to the signal tokens it needs beyond the always
-present ``url``/``status``. Checks absent from this map either need only those
(e.g. #2 status codes, #50 malformed URLs) or gate themselves internally
(robots/sitemap fetched live; CWV via Lighthouse; RENDER via the browser;
EXTERNAL via GSC/backlinks).
"""

from __future__ import annotations

# Signal tokens.
TITLE = "title"
META_DESC = "meta_desc"
H1 = "h1"
META_ROBOTS = "meta_robots"
CANONICAL = "canonical"
REDIRECT = "redirect"
CONTENT_ENCODING = "content_encoding"
VIEWPORT = "viewport"
BODY_TEXT = "body_text"
MULTIPLES = "multiples"            # Title 2 / Meta Description 2 / Canonical 2 present
HEAD_STRUCTURE = "head_structure"  # in-head / outside-head / multiple-head (raw HTML)
LINKS = "links"                    # per-page link graph (hrefs)
SCRIPTS = "scripts"                # per-page <script src> / <link rel=stylesheet>
IMAGES = "images"                  # per-page <img> sources
STRUCTURED_DATA = "structured_data"
RESOURCE_HINTS = "resource_hints"  # link rel=preconnect / preload

CHECK_SIGNALS: dict[int, tuple[str, ...]] = {
    1: (BODY_TEXT,),
    6: (CANONICAL,),
    7: (HEAD_STRUCTURE,),
    8: (CANONICAL,),
    9: (CANONICAL,),
    10: (CANONICAL,),
    11: (CANONICAL,),
    12: (CANONICAL,),
    13: (SCRIPTS,),
    14: (SCRIPTS,),
    18: (LINKS,),
    20: (LINKS,),
    23: (HEAD_STRUCTURE,),
    24: (CANONICAL,),
    28: (HEAD_STRUCTURE,),   # CWV-partial static fallback: sync scripts/styles in <head>
    29: (IMAGES,),
    30: (STRUCTURED_DATA,),
    31: (STRUCTURED_DATA,),
    32: (STRUCTURED_DATA,),
    33: (HEAD_STRUCTURE,),
    34: (H1,),
    37: (BODY_TEXT,),
    40: (CANONICAL,),
    41: (IMAGES,),
    42: (SCRIPTS,),
    43: (SCRIPTS,),
    44: (CONTENT_ENCODING,),
    45: (SCRIPTS,),
    46: (SCRIPTS,),
    47: (SCRIPTS,),
    49: (LINKS,),
    51: (RESOURCE_HINTS,),
    52: (VIEWPORT,),
    53: (IMAGES,),
    54: (SCRIPTS,),
    55: (META_DESC,),
    56: (MULTIPLES,),
    59: (REDIRECT,),
    60: (REDIRECT,),
    61: (META_ROBOTS,),
    62: (META_ROBOTS,),
    63: (META_ROBOTS, CANONICAL),
    64: (LINKS,),
    65: (LINKS,),
    66: (LINKS,),
    67: (META_ROBOTS,),
    68: (META_ROBOTS,),
    74: (HEAD_STRUCTURE,),
    76: (MULTIPLES,),
    79: (CANONICAL,),
    80: (IMAGES,),
    85: (IMAGES,),
    86: (RESOURCE_HINTS,),
    89: (SCRIPTS,),
    91: (LINKS,),
}

# Tier-gated checks handle their own availability (CWV via Lighthouse, RENDER via
# the browser). When that live source is active, the signal gate is bypassed so
# the real verdict can be produced.
LIVE_TIER_BYPASS = {"CWV": "has_lighthouse", "RENDER": "has_render"}
