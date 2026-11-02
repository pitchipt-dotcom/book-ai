#!/usr/bin/env python3
"""Create reproducible reports of the current SQLite literal-search quality.

No model, GPU, OCR, source PDF, or production server is used.  Results are
derived artifacts for human relevance review, not automatic clinical advice.
"""
import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

SEARCH_DIR = Path(__file__).resolve().parent
DEFAULT_QUERIES = SEARCH_DIR / "eval_queries.json"
DEFAULT_JSON = SEARCH_DIR / "eval_results.json"
DEFAULT_MD = SEARCH_DIR / "eval_results.md"


def load_app():
    spec = importlib.util.spec_from_file_location("book_ai_search_app", SEARCH_DIR / "app.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    return app


def compact(text, limit=180):
    return " ".join(text.split())[:limit]


def markdown(report):
    lines = [
        "# 現行SQLite検索 評価結果",
        "",
        "このレポートはモデルを使用せず、現行の文字列部分一致検索の出力を固定クエリで記録したものです。",
        "自然文・複数語はそのまま一つの文字列として検索するため、0件は想定どおりの重要な評価結果です。",
        "",
        f"生成日時 (UTC): {report['generated_at']}",
        f"1クエリあたりの表示上限: {report['top_k']}",
        "",
        "## サマリー",
        "",
        "|種別|クエリ数|0件|上位結果あり|",
        "|---|---:|---:|---:|",
    ]
    for kind, values in report["summary"]["by_type"].items():
        lines.append(f"|{kind}|{values['queries']}|{values['zero_hit_queries']}|{values['queries'] - values['zero_hit_queries']}|")
    lines.extend(["", "## クエリ別の上位根拠", ""])
    for item in report["results"]:
        lines.extend([
            f"### {item['id']}",
            "",
            f"- 種別: `{item['type']}`",
            f"- クエリ: `{item['query']}`",
            f"- レビュー観点: {', '.join(item['expected_concepts'])}",
            f"- ヒットページ数: {item['count']}",
            "",
        ])
        if not item["top_results"]:
            lines.extend(["_上位結果なし（現行検索ではクエリ文字列の連続部分一致が必要）。_", ""])
            continue
        lines.extend([
            "|順位|書名|実PDF|PDF p.|書籍内PDF p.|snippet|citation_id|",
            "|---:|---|---|---:|---:|---|---|",
        ])
        for rank, result in enumerate(item["top_results"], start=1):
            lines.append(
                f"|{rank}|{result['book_title']}|{result['source_file']}|{result['pdf_page']}|"
                f"{result['book_pdf_page']}|{compact(result['snippet']).replace('|', '\\|')}|{result['citation_id']}|"
            )
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")

    query_set = json.loads(args.queries.read_text(encoding="utf-8"))
    app = load_app()
    results = []
    by_type = {}
    for query in query_set["queries"]:
        count, top_results = app.search_pages(query["query"], None, 0, args.top_k)
        results.append({
            "id": query["id"], "type": query["type"], "query": query["query"],
            "expected_concepts": query["expected_concepts"], "count": count,
            "top_results": top_results,
        })
        stats = by_type.setdefault(query["type"], {"queries": 0, "zero_hit_queries": 0})
        stats["queries"] += 1
        stats["zero_hit_queries"] += int(count == 0)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query_set": str(args.queries), "top_k": args.top_k,
        "summary": {"queries": len(results), "zero_hit_queries": sum(item["count"] == 0 for item in results), "by_type": by_type},
        "results": results,
    }
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_out.write_text(markdown(report), encoding="utf-8")
    print(f"written: {args.json_out}")
    print(f"written: {args.markdown_out}")
    print(f"queries={len(results)} zero_hit={report['summary']['zero_hit_queries']}")


if __name__ == "__main__":
    main()
