from pathlib import Path

from reuse_figure8_preparation import PREPARATION_FILES, reuse_preparation


def test_reuse_preparation_preserves_receipt_contents_and_copies_readback(tmp_path: Path):
    source = tmp_path / "prepared"
    readback = source / "readback"
    readback.mkdir(parents=True)
    for name in PREPARATION_FILES:
        (source / name).write_text(f"{name}:observed_at=100\n")
    (readback / "contact.script").write_text("package\n")
    destination = reuse_preparation(source, tmp_path / "attempt")
    assert (destination / "software_baseline_receipt.json").read_text() == "software_baseline_receipt.json:observed_at=100\n"
    assert (destination / "readback" / "contact.script").read_text() == "package\n"
