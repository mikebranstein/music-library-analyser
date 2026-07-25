from __future__ import annotations

from pathlib import Path

from scripts._common import read_jsonl


def test_read_jsonl_skips_malformed_and_non_object_lines(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "sample.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                '{"ok": true}',
                "not-json",
                "[1, 2, 3]",
                '{"another": 1}',
                "",
            ]
        ),
        encoding="utf-8",
    )

    records = read_jsonl(jsonl_path)
    assert records == [{"ok": True}, {"another": 1}]
