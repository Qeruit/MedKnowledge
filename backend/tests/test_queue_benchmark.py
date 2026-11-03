"""Small checks for the reusable measurement helpers, without external calls."""
from scripts.queue_benchmark import make_document, percentile


def test_percentile_linear_interpolation():
    assert percentile([4,1,3,2],.5) == 2.5
    assert abs(percentile([4,1,3,2],.95)-3.85) < 1e-12
    assert percentile([7],.95) == 7


def test_document_sizes_and_markers(tmp_path):
    for kib in (4,32,128):
        path=tmp_path/f"size-{kib}.md"
        make_document(path,kib,"SYNTHETIC-NONMEDICAL")
        assert kib*1024 <= path.stat().st_size < kib*1024+2500
        assert "SYNTHETIC-NONMEDICAL" in path.read_text(encoding="utf-8")
