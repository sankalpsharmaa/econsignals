"""
normalize.py
------------
Title and author name normalization for cross-source paper deduplication.
All functions are pure and depend only on the standard library.
"""

import hashlib
import re
import unicodedata


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    """NFKD decomposition, drop non-ASCII combining marks."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


_WORKING_PAPER_PREFIXES: list[re.Pattern[str]] = [
    # More specific patterns first so "nber working paper" is consumed whole
    re.compile(r"nber\s+working\s+paper\s*(no\.?\s*)?\d*", re.IGNORECASE),
    re.compile(r"working\s+paper\s*(no\.?\s*)?\d*", re.IGNORECASE),
    re.compile(r"discussion\s+paper\s*(no\.?\s*)?\d*", re.IGNORECASE),
    re.compile(r"technical\s+report\s*\d*", re.IGNORECASE),
    re.compile(r"research\s+paper\s*\d*", re.IGNORECASE),
    # Catch bare source abbreviations left over after longer patterns fire
    re.compile(r"\bnber\b", re.IGNORECASE),
    re.compile(r"\bcepr\b", re.IGNORECASE),
    re.compile(r"\bimf\b", re.IGNORECASE),
    re.compile(r"\bwber\b", re.IGNORECASE),
]

# Suffixes to remove from author names (word-boundary anchored)
_NAME_SUFFIX_PATTERN = re.compile(
    r"\b(jr\.?|sr\.?|ii|iii|iv|phd|ph\.d\.?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """Normalize a paper title for deduplication matching.

    Steps applied in order:
    1. Unicode NFKD normalization, strip accents.
    2. Lowercase.
    3. Strip known working-paper series prefixes (e.g. "NBER Working Paper 1234").
    4. Remove all punctuation, keeping only alphanumeric characters and spaces.
    5. Collapse internal whitespace, strip leading/trailing whitespace.

    Args:
        title: Raw paper title string.

    Returns:
        Normalized title string suitable for exact or fuzzy matching.

    Example:
        >>> normalize_title("NBER Working Paper No. 31705: Inflation and Wages")
        'inflation and wages'
    """
    s = _strip_accents(title)
    s = s.lower()

    # Strip working-paper prefixes wherever they appear (may be at start or
    # embedded after a colon).  Apply repeatedly until stable in case multiple
    # patterns overlap.
    changed = True
    while changed:
        prev = s
        for pattern in _WORKING_PAPER_PREFIXES:
            s = pattern.sub("", s)
        changed = s != prev

    # Drop punctuation; keep alphanumeric and whitespace
    s = re.sub(r"[^a-z0-9\s]", " ", s)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_author_name(name: str) -> str:
    """Normalize an author name for deduplication matching.

    Steps applied in order:
    1. Unicode NFKD normalization, strip accents.
    2. Lowercase.
    3. Remove common name suffixes (jr, sr, ii, iii, phd, ph.d., ...).
    4. Remove punctuation except hyphens within name parts.
    5. Collapse whitespace, strip.

    The function does *not* reorder "Last, First" into "First Last"; use
    :func:`extract_last_name` when only the last name is needed.

    Args:
        name: Raw author name string.

    Returns:
        Normalized name string.

    Example:
        >>> normalize_author_name("O'Brien, Sean Jr.")
        'obrien sean'
    """
    s = _strip_accents(name)
    s = s.lower()

    # Remove name suffixes
    s = _NAME_SUFFIX_PATTERN.sub("", s)

    # Handle "Last, First" format: drop the comma so order becomes "Last First"
    s = s.replace(",", " ")

    # Remove punctuation except hyphens; apostrophes (O'Brien) are also dropped
    s = re.sub(r"[^a-z0-9\s\-]", "", s)

    # Remove lone hyphens or hyphens at word boundaries left by the above
    s = re.sub(r"(?<!\w)-|-(?!\w)", " ", s)

    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_last_name(name: str) -> str:
    """Extract the last (family) name from a full name string.

    Handles the following formats:
    - "First Last"
    - "Last, First"
    - "First Middle Last"

    Args:
        name: Raw author name string.

    Returns:
        Lowercase, accent-stripped last name.

    Example:
        >>> extract_last_name("John Maynard Keynes")
        'keynes'
        >>> extract_last_name("Keynes, John Maynard")
        'keynes'
    """
    normalized = normalize_author_name(name)

    # Detect "Last, First" from original (comma present before normalization)
    if "," in name:
        # Part before the first comma is the last name
        last_raw = name.split(",", 1)[0]
        return normalize_author_name(last_raw).split()[0] if normalize_author_name(last_raw).split() else ""

    parts = normalized.split()
    if not parts:
        return ""
    return parts[-1]


def sorted_author_lastnames(authors: list[str]) -> list[str]:
    """Extract, normalize, and sort last names from a list of author name strings.

    Used to build the author component of a canonical paper ID so that author
    ordering differences across data sources do not produce distinct IDs.

    Args:
        authors: List of raw author name strings.

    Returns:
        Sorted list of lowercase last name strings.

    Example:
        >>> sorted_author_lastnames(["John Smith", "Alice Zhao"])
        ['smith', 'zhao']
    """
    last_names = [extract_last_name(a) for a in authors]
    return sorted(ln for ln in last_names if ln)


def canonical_paper_id(title: str, authors: list[str]) -> str:
    """Generate a short canonical ID for a paper.

    The ID is the first 16 hex characters of:
        SHA-256( normalized_title + "|" + ",".join(sorted_author_lastnames) )

    This makes cross-source deduplication resilient to minor title variations
    and author ordering differences.

    Args:
        title: Raw paper title.
        authors: List of raw author name strings.

    Returns:
        16-character lowercase hex string.

    Example:
        >>> canonical_paper_id("A Theory of Wages", ["Smith, John", "Alice Zhao"])
        '...'  # deterministic hex string
    """
    norm_title = normalize_title(title)
    norm_authors = ",".join(sorted_author_lastnames(authors))
    payload = f"{norm_title}|{norm_authors}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def title_token_set(title: str) -> set[str]:
    """Return the set of tokens from a normalized title.

    Intended for use with :func:`jaccard_similarity` to detect near-duplicate
    titles (e.g. those that differ only by subtitle or minor wording).

    Args:
        title: Raw or pre-normalized title string.

    Returns:
        Set of non-empty token strings.

    Example:
        >>> title_token_set("Inflation and Wages: New Evidence")
        {'inflation', 'and', 'wages', 'new', 'evidence'}
    """
    return set(normalize_title(title).split())


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Compute Jaccard similarity between two sets.

    J(A, B) = |A ∩ B| / |A ∪ B|

    Args:
        set_a: First set.
        set_b: Second set.

    Returns:
        Float in [0.0, 1.0]. Returns 0.0 when both sets are empty.

    Example:
        >>> jaccard_similarity({'a', 'b', 'c'}, {'b', 'c', 'd'})
        0.5
    """
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def author_lastnames_overlap(authors_a: list[str], authors_b: list[str]) -> int:
    """Count shared last names between two author lists after normalization.

    Useful as a secondary signal when title Jaccard similarity is high but not
    conclusive (e.g. common short titles).

    Args:
        authors_a: First list of raw author name strings.
        authors_b: Second list of raw author name strings.

    Returns:
        Count of last names appearing in both lists.

    Example:
        >>> author_lastnames_overlap(["John Smith"], ["Jane Smith", "Bob Jones"])
        1
    """
    set_a = set(sorted_author_lastnames(authors_a))
    set_b = set(sorted_author_lastnames(authors_b))
    return len(set_a & set_b)


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample_titles = [
        "NBER Working Paper No. 31705: Inflation and the Labor Market",
        "Discussion Paper No. 42 -- Fiscal Policy Under Uncertainty",
        "Working Paper: Monetary Policy Transmission in Emerging Markets",
        "Technical Report 7: Trade, Technology, and Inequality",
        "Café Owners and Macroeconomic Shocks: Evidence from Zürich",
    ]

    sample_authors = [
        "Keynes, John Maynard",
        "Milton Friedman",
        "O'Brien, Sean Jr.",
        "Héctor García-López",
        "Smith, John PhD",
        "Anna-Maria Müller III",
    ]

    print("=== Title normalization ===")
    for t in sample_titles:
        print(f"  IN : {t!r}")
        print(f"  OUT: {normalize_title(t)!r}")
        print()

    print("=== Author normalization ===")
    for a in sample_authors:
        print(f"  IN        : {a!r}")
        print(f"  normalized: {normalize_author_name(a)!r}")
        print(f"  last name : {extract_last_name(a)!r}")
        print()

    print("=== Canonical ID ===")
    authors = ["John Maynard Keynes", "Milton Friedman"]
    title = "NBER Working Paper No. 100: Inflation Expectations"
    cid = canonical_paper_id(title, authors)
    print(f"  title  : {title!r}")
    print(f"  authors: {authors}")
    print(f"  id     : {cid!r}")
    print()

    print("=== Jaccard similarity ===")
    ta = title_token_set("Inflation and Wages: New Evidence from the US")
    tb = title_token_set("Inflation and Wages New Evidence")
    print(f"  set_a   : {sorted(ta)}")
    print(f"  set_b   : {sorted(tb)}")
    print(f"  jaccard : {jaccard_similarity(ta, tb):.3f}")
    print()

    print("=== Author overlap ===")
    a1 = ["John Smith", "Alice Zhao", "Bob Jones"]
    a2 = ["Smith, John", "Carol White"]
    print(f"  authors_a : {a1}")
    print(f"  authors_b : {a2}")
    print(f"  overlap   : {author_lastnames_overlap(a1, a2)}")
