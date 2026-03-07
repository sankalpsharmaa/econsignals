"""Unit tests for econsignals.lib.normalize."""


from econsignals.lib.normalize import (
    author_lastnames_overlap,
    canonical_paper_id,
    extract_last_name,
    jaccard_similarity,
    normalize_author_name,
    normalize_title,
    sorted_author_lastnames,
    title_token_set,
)


class TestNormalizeTitle:
    """Tests for normalize_title()."""

    def test_strips_nber_prefix(self):
        assert normalize_title("NBER Working Paper No. 31705: Inflation and Wages") == "inflation and wages"

    def test_strips_working_paper_prefix(self):
        assert normalize_title("Working Paper: Monetary Policy Transmission") == "monetary policy transmission"

    def test_strips_discussion_paper(self):
        assert normalize_title("Discussion Paper No. 42 -- Fiscal Policy") == "fiscal policy"

    def test_strips_technical_report(self):
        assert normalize_title("Technical Report 7: Trade and Inequality") == "trade and inequality"

    def test_strips_accents(self):
        assert normalize_title("Café Owners and Macroeconomic Shocks: Evidence from Zürich") == "cafe owners and macroeconomic shocks evidence from zurich"

    def test_collapses_whitespace(self):
        assert normalize_title("  Multiple   Spaces   ") == "multiple spaces"

    def test_removes_punctuation(self):
        # Apostrophes and periods become spaces; alphanumeric kept
        assert normalize_title("What's New? Evidence from the U.S.") == "what s new evidence from the u s"

    def test_empty_string(self):
        assert normalize_title("") == ""


class TestNormalizeAuthorName:
    """Tests for normalize_author_name()."""

    def test_last_first_format(self):
        assert normalize_author_name("Keynes, John Maynard") == "keynes john maynard"

    def test_first_last_format(self):
        assert normalize_author_name("Milton Friedman") == "milton friedman"

    def test_strips_jr_suffix(self):
        assert normalize_author_name("O'Brien, Sean Jr.") == "obrien sean"

    def test_strips_phd_suffix(self):
        assert normalize_author_name("Smith, John PhD") == "smith john"

    def test_strips_accents(self):
        # Hyphens within names are preserved
        assert normalize_author_name("Héctor García-López") == "hector garcia-lopez"

    def test_empty_string(self):
        assert normalize_author_name("") == ""


class TestExtractLastName:
    """Tests for extract_last_name()."""

    def test_first_last_format(self):
        assert extract_last_name("John Maynard Keynes") == "keynes"

    def test_last_comma_first_format(self):
        assert extract_last_name("Keynes, John Maynard") == "keynes"

    def test_single_name(self):
        assert extract_last_name("Madonna") == "madonna"

    def test_empty_string(self):
        assert extract_last_name("") == ""


class TestSortedAuthorLastnames:
    """Tests for sorted_author_lastnames()."""

    def test_sorts_and_extracts(self):
        assert sorted_author_lastnames(["John Smith", "Alice Zhao"]) == ["smith", "zhao"]

    def test_handles_ordering_differences(self):
        a = sorted_author_lastnames(["Keynes, John", "Friedman, Milton"])
        b = sorted_author_lastnames(["John Keynes", "Milton Friedman"])
        assert a == b == ["friedman", "keynes"]

    def test_empty_list(self):
        assert sorted_author_lastnames([]) == []


class TestCanonicalPaperId:
    """Tests for canonical_paper_id()."""

    def test_deterministic(self):
        cid1 = canonical_paper_id("A Theory of Wages", ["Smith, John", "Alice Zhao"])
        cid2 = canonical_paper_id("A Theory of Wages", ["Smith, John", "Alice Zhao"])
        assert cid1 == cid2

    def test_same_id_despite_author_order(self):
        cid1 = canonical_paper_id("Inflation", ["Alice", "Bob"])
        cid2 = canonical_paper_id("Inflation", ["Bob", "Alice"])
        assert cid1 == cid2

    def test_different_titles_different_ids(self):
        cid1 = canonical_paper_id("Title A", ["Smith"])
        cid2 = canonical_paper_id("Title B", ["Smith"])
        assert cid1 != cid2

    def test_returns_16_char_hex(self):
        cid = canonical_paper_id("Test", ["Author"])
        assert len(cid) == 16
        assert all(c in "0123456789abcdef" for c in cid)


class TestTitleTokenSet:
    """Tests for title_token_set()."""

    def test_splits_into_tokens(self):
        tokens = title_token_set("Inflation and Wages: New Evidence")
        assert tokens == {"inflation", "and", "wages", "new", "evidence"}

    def test_normalizes_title(self):
        tokens = title_token_set("NBER WP 123: Inflation")
        assert "inflation" in tokens
        assert "nber" not in tokens


class TestJaccardSimilarity:
    """Tests for jaccard_similarity()."""

    def test_identical_sets(self):
        assert jaccard_similarity({"a", "b", "c"}, {"a", "b", "c"}) == 1.0

    def test_disjoint_sets(self):
        assert jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        # |intersection|=2, |union|=4
        assert jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"}) == 0.5

    def test_both_empty(self):
        assert jaccard_similarity(set(), set()) == 0.0


class TestAuthorLastnamesOverlap:
    """Tests for author_lastnames_overlap()."""

    def test_counts_shared_last_names(self):
        assert author_lastnames_overlap(["John Smith"], ["Jane Smith", "Bob Jones"]) == 1

    def test_handles_format_differences(self):
        assert author_lastnames_overlap(["John Smith"], ["Smith, John"]) == 1

    def test_no_overlap(self):
        assert author_lastnames_overlap(["Alice Zhao"], ["Bob Jones"]) == 0

    def test_multiple_overlap(self):
        a1 = ["John Smith", "Alice Zhao", "Bob Jones"]
        a2 = ["Smith, John", "Carol White", "Zhao, Alice"]
        assert author_lastnames_overlap(a1, a2) == 2
