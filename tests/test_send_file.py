"""Tests for the send_file (document/file) tool and MIME detection."""
import base64
import types

from ouroboros.tools.core import _send_file, _detect_document_mime, _MAX_DOCUMENT_FILE_BYTES


def _make_ctx(chat_id=123):
    return types.SimpleNamespace(
        current_chat_id=chat_id,
        pending_events=[],
    )


class TestSendFile:
    def test_file_path_reads_document(self, tmp_path):
        doc = tmp_path / "report.csv"
        doc.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

        ctx = _make_ctx()
        result = _send_file(ctx, file_path=str(doc), caption="quarterly report")

        assert "OK" in result
        assert len(ctx.pending_events) == 1
        event = ctx.pending_events[0]
        assert event["type"] == "send_document"
        assert event["mime"] == "text/csv"
        assert event["filename"] == "report.csv"
        assert event["caption"] == "quarterly report"
        assert event["file_base64"] == base64.b64encode(doc.read_bytes()).decode()

    def test_unknown_extension_falls_back_to_octet_stream(self, tmp_path):
        blob = tmp_path / "data.bin"
        blob.write_bytes(b"\x00\x01\x02\x03")

        ctx = _make_ctx()
        result = _send_file(ctx, file_path=str(blob))

        assert "OK" in result
        assert ctx.pending_events[0]["mime"] == "application/octet-stream"

    def test_chat_zero_is_valid(self, tmp_path):
        doc = tmp_path / "note.txt"
        doc.write_text("hi", encoding="utf-8")

        ctx = _make_ctx(chat_id=0)
        result = _send_file(ctx, file_path=str(doc))

        assert "OK" in result
        assert ctx.pending_events[0]["chat_id"] == 0

    def test_no_active_chat_returns_error(self, tmp_path):
        doc = tmp_path / "note.txt"
        doc.write_text("hi", encoding="utf-8")

        ctx = _make_ctx(chat_id=None)
        result = _send_file(ctx, file_path=str(doc))

        assert "no active chat" in result.lower()
        assert ctx.pending_events == []

    def test_file_not_found(self):
        ctx = _make_ctx()
        result = _send_file(ctx, file_path="/nonexistent/report.pdf")
        assert "not found" in result.lower()

    def test_directory_is_rejected(self, tmp_path):
        ctx = _make_ctx()
        result = _send_file(ctx, file_path=str(tmp_path))
        assert "not found" in result.lower()
        assert ctx.pending_events == []

    def test_file_too_large(self, tmp_path):
        big = tmp_path / "huge.bin"
        big.write_bytes(b"\x00" * (_MAX_DOCUMENT_FILE_BYTES + 1))

        ctx = _make_ctx()
        result = _send_file(ctx, file_path=str(big))
        assert "too large" in result.lower()

    def test_no_input_returns_error(self):
        ctx = _make_ctx()
        result = _send_file(ctx)
        assert "provide" in result.lower()


class TestDetectDocumentMime:
    def test_pdf_extension(self):
        assert _detect_document_mime("report.pdf") == "application/pdf"

    def test_csv_extension(self):
        assert _detect_document_mime("data.csv") == "text/csv"

    def test_unknown_extension(self):
        assert _detect_document_mime("blob.unknownext") == "application/octet-stream"
