"""
H6 — _collect_8k_text() birim testleri.

Mock filing/attachment nesneleri kullanılır — gerçek SEC/EDGAR çağrısı yapılmaz.
edgartools'un gerçek API'si: att.document (lowercase filename), att.text() (str|None).
"""
import pytest


# ─── Mock yardımcıları ────────────────────────────────────
class MockAttachment:
    def __init__(self, document, text_return):
        self.document = document
        self._text = text_return

    def text(self):
        return self._text

    def markdown(self):
        return self._text


class MockFiling:
    def __init__(self, primary_text, attachments=None, *, no_attachments_attr=False):
        self._primary = primary_text
        self._attachments = attachments or []
        self._no_attachments_attr = no_attachments_attr

    def text(self):
        return self._primary

    def markdown(self):
        return self._primary

    @property
    def attachments(self):
        if self._no_attachments_attr:
            raise AttributeError("attachments not supported")
        return self._attachments


# ─── Testler ─────────────────────────────────────────────
class TestCollect8kText:

    def test_no_attachments_returns_primary_only(self, bot):
        """Ek yoksa yalnızca primary doc metni döner."""
        filing = MockFiling(primary_text="Primary content", attachments=[])
        result = bot._collect_8k_text(filing)
        assert result == "Primary content"

    def test_ex99_attachments_appended(self, bot):
        """EX-99.1 ve EX-99.2 ekleri sırayla primary doc'a eklenir."""
        filing = MockFiling(
            primary_text="Cover page",
            attachments=[
                MockAttachment("ex-99_1.htm", "Earnings release text"),
                MockAttachment("ex-99_2.htm", "Supplemental tables"),
            ],
        )
        result = bot._collect_8k_text(filing)
        assert "Cover page" in result
        assert "Earnings release text" in result
        assert "Supplemental tables" in result
        # Sıra korunmalı
        assert result.index("Earnings release text") < result.index("Supplemental tables")

    def test_non_ex99_attachments_excluded(self, bot):
        """EX-10 ve diğer ekler dahil edilmez."""
        filing = MockFiling(
            primary_text="Primary",
            attachments=[
                MockAttachment("ex-10_1.htm", "Material contract"),
                MockAttachment("ex-99_1.htm", "Press release"),
                MockAttachment("ex-21.htm",   "Subsidiaries list"),
            ],
        )
        result = bot._collect_8k_text(filing)
        assert "Press release" in result
        assert "Material contract" not in result
        assert "Subsidiaries list" not in result

    def test_att_text_none_skipped_with_warning(self, bot, caplog):
        """att.text() None döndürürse o ek atlanır, diğerleri devam eder."""
        import logging
        filing = MockFiling(
            primary_text="Primary",
            attachments=[
                MockAttachment("ex-99_1.htm", None),          # None → atla
                MockAttachment("ex-99_2.htm", "Good content"),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="bot"):
            result = bot._collect_8k_text(filing)
        assert "Good content" in result
        assert "ex-99_1.htm" in caplog.text    # warning loglandı

    def test_att_text_empty_string_skipped(self, bot):
        """att.text() boş string döndürürse ek atlanır."""
        filing = MockFiling(
            primary_text="Primary",
            attachments=[
                MockAttachment("ex-99_1.htm", ""),
                MockAttachment("ex-99_2.htm", "Real content"),
            ],
        )
        result = bot._collect_8k_text(filing)
        assert "Real content" in result
        # Boş ek separator'ı da eklenmemeli
        assert "ex-99_1.htm" not in result

    def test_attachments_attribute_error_falls_back_to_primary(self, bot, caplog):
        """filing.attachments AttributeError → primary doc döner, çökmez."""
        import logging
        filing = MockFiling(primary_text="Primary only", no_attachments_attr=True)
        with caplog.at_level(logging.WARNING, logger="bot"):
            result = bot._collect_8k_text(filing)
        assert result == "Primary only"
        assert "attachments not available" in caplog.text

    def test_separator_format_in_output(self, bot):
        """Her ek üç-tire separator ile ayrılır: '--- filename ---'."""
        filing = MockFiling(
            primary_text="Cover",
            attachments=[MockAttachment("ex-99_1.htm", "Release body")],
        )
        result = bot._collect_8k_text(filing)
        assert "--- ex-99_1.htm ---" in result

    def test_primary_text_none_returns_empty(self, bot):
        """filing.text() None döndürürse (ağ hatası değil) boş string başlangıç olur."""
        filing = MockFiling(primary_text=None, attachments=[
            MockAttachment("ex-99_1.htm", "Exhibit text"),
        ])
        result = bot._collect_8k_text(filing)
        # EX-99.1 hâlâ dahil edilmeli; primary None → "" gibi davranır
        assert "Exhibit text" in result

    def test_case_insensitive_ex99_matching(self, bot):
        """EX-99 büyük/küçük harf farkı gözetmeksizin eşleşmeli."""
        filing = MockFiling(
            primary_text="Cover",
            attachments=[
                MockAttachment("EX-99_1.HTM", "Upper case exhibit"),
                MockAttachment("Ex-99_2.htm", "Mixed case exhibit"),
            ],
        )
        result = bot._collect_8k_text(filing)
        assert "Upper case exhibit" in result
        assert "Mixed case exhibit" in result

    def test_attachment_text_raises_exception_skipped(self, bot, caplog):
        """att.text() exception fırlatırsa ek atlanır, diğerleri devam eder."""
        import logging

        class BadAttachment:
            document = "ex-99_1.htm"
            def text(self):
                raise RuntimeError("network error")

        filing = MockFiling(
            primary_text="Primary",
            attachments=[
                BadAttachment(),
                MockAttachment("ex-99_2.htm", "Good exhibit"),
            ],
        )
        with caplog.at_level(logging.WARNING, logger="bot"):
            result = bot._collect_8k_text(filing)
        assert "Good exhibit" in result
        assert "attachment fetch error" in caplog.text
