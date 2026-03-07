"""Unit tests for econsignals.lib.dedup."""


from econsignals.lib.dedup import merge_paper_metadata


class TestMergePaperMetadata:
    """Tests for merge_paper_metadata() - pure merge logic for deduplication."""

    def test_no_updates_when_existing_has_all(self):
        existing = {
            "doi": "10.1234/foo",
            "abstract": "Long abstract here",
            "paper_type": "journal_article",
            "jel_codes": ["O1", "R1"],
            "keywords": ["urban", "housing"],
            "published_at": "2023-01-15",
        }
        new = {
            "doi": "10.1234/foo",
            "abstract": "Short",
            "paper_type": "working_paper",
        }
        assert merge_paper_metadata(existing, new) == {}

    def test_adds_doi_when_existing_missing(self):
        existing = {"doi": None, "abstract": ""}
        new = {"doi": "10.1234/new"}
        assert merge_paper_metadata(existing, new) == {"doi": "10.1234/new"}

    def test_prefers_longer_abstract(self):
        existing = {"abstract": "Short"}
        new = {"abstract": "Much longer abstract with more detail"}
        assert merge_paper_metadata(existing, new) == {
            "abstract": "Much longer abstract with more detail"
        }

    def test_does_not_replace_longer_with_shorter(self):
        existing = {"abstract": "Long abstract stays"}
        new = {"abstract": "Short"}
        assert merge_paper_metadata(existing, new) == {}

    def test_upgrades_paper_type_journal_over_working(self):
        existing = {"paper_type": "working_paper"}
        new = {"paper_type": "journal_article"}
        assert merge_paper_metadata(existing, new) == {"paper_type": "journal_article"}

    def test_does_not_downgrade_paper_type(self):
        existing = {"paper_type": "journal_article"}
        new = {"paper_type": "working_paper"}
        assert merge_paper_metadata(existing, new) == {}

    def test_merges_jel_codes_order_preserving(self):
        existing = {"jel_codes": ["O1", "R1"]}
        new = {"jel_codes": ["O1", "J2"]}  # O1 duplicate, J2 new
        result = merge_paper_metadata(existing, new)
        assert result["jel_codes"] == ["O1", "R1", "J2"]

    def test_merges_keywords_case_insensitive_dedup(self):
        existing = {"keywords": ["Urban", "Housing"]}
        new = {"keywords": ["urban", "Land"]}  # urban duplicate
        result = merge_paper_metadata(existing, new)
        assert result["keywords"] == ["Urban", "Housing", "Land"]

    def test_published_at_prefers_earliest(self):
        existing = {"published_at": "2023-06-15"}
        new = {"published_at": "2023-01-01"}
        assert merge_paper_metadata(existing, new) == {"published_at": "2023-01-01"}

    def test_published_at_adds_when_missing(self):
        existing = {"published_at": None}
        new = {"published_at": "2023-09"}
        assert merge_paper_metadata(existing, new) == {"published_at": "2023-09"}

    def test_combined_updates(self):
        existing = {
            "doi": None,
            "abstract": "Short",
            "paper_type": "working_paper",
            "jel_codes": [],
            "keywords": [],
            "published_at": None,
        }
        new = {
            "doi": "10.1234/xyz",
            "abstract": "Longer abstract text",
            "paper_type": "journal_article",
            "jel_codes": ["O1"],
            "keywords": ["development"],
            "published_at": "2023-01",
        }
        result = merge_paper_metadata(existing, new)
        assert result["doi"] == "10.1234/xyz"
        assert result["abstract"] == "Longer abstract text"
        assert result["paper_type"] == "journal_article"
        assert result["jel_codes"] == ["O1"]
        assert result["keywords"] == ["development"]
        assert result["published_at"] == "2023-01"
