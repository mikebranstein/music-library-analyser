from __future__ import annotations

from pathlib import Path

from scripts._common import read_jsonl, run_with_progress


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


def test_run_with_progress_on_result_sequential() -> None:
    items = [1, 2, 3]
    seen: list[tuple[int, int]] = []

    results = run_with_progress(
        items, lambda x: x * 10, max_workers=1, on_result=lambda item, res: seen.append((item, res))
    )

    assert results == [10, 20, 30]
    # on_result is called once per item, in order, with (item, result).
    assert seen == [(1, 10), (2, 20), (3, 30)]


def test_run_with_progress_on_result_parallel_preserves_input_order() -> None:
    items = [1, 2, 3, 4]
    seen: list[tuple[int, int]] = []

    results = run_with_progress(
        items,
        lambda x: x * 10,
        max_workers=4,
        on_result=lambda item, res: seen.append((item, res)),
    )

    # Returned results stay in input order regardless of completion order.
    assert results == [10, 20, 30, 40]
    # Every item is reported exactly once via on_result.
    assert sorted(seen) == [(1, 10), (2, 20), (3, 30), (4, 40)]
