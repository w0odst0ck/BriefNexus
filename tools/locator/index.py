"""汇总索引 — 批量检测并产出 index.json。"""

import csv
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from pathlib import Path

from tools.locator.detector import detect, DetectorResult


@dataclass
class PageEntry:
    page: int
    signals: list[str]
    table_title: str = ""
    confidence: str = "medium"


@dataclass
class PdfIndex:
    filename: str
    category: str
    route: str
    total_pages: int
    status: str = "success"
    parameter_pages: list[PageEntry] = field(default_factory=list)
    error: str | None = None

    @property
    def summary(self) -> dict:
        if self.total_pages == 0:
            return {"param_pages": 0, "total_pages": 0, "savings_pct": 0}
        pages = [p.page for p in self.parameter_pages]
        return {
            "param_pages": len(pages),
            "total_pages": self.total_pages,
            "pages_found": sorted(pages),
            "savings_pct": round(
                (1 - len(pages) / self.total_pages) * 100, 1
            ),
        }


@dataclass
class Index:
    metadata: dict
    results: list[dict] = field(default_factory=list)

    def add_pdf(self, pdf_index: PdfIndex) -> None:
        d = asdict(pdf_index)
        d["summary"] = pdf_index.summary
        self.results.append(d)

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)


def run(datasets_dir: str, output_path: str, mode: str = "text") -> Index:
    """Run locator on all PDFs in sources.csv.

    Args:
        datasets_dir: 包含 sources.csv 和 PDF 文件的目录。
        output_path: index.json 输出路径。
        mode: "text" | "ocr" | "auto"

    Returns:
        Index 对象（已写入 output_path）。
    """
    datasets_dir = Path(datasets_dir)
    sources_csv = datasets_dir / "sources.csv"

    # Read sources.csv
    rows: list[dict] = []
    with open(sources_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    index = Index(metadata={
        "project": "pdf-extract-bench",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_pdfs": len(rows),
        "detection_version": "v1",
        "mode": mode,
    })

    print(f"📋 Detecting {len(rows)} PDFs (mode={mode})...")
    print()

    for row in rows:
        filename = row["filename"]
        category = row.get("category", "lighting")
        pdf_path = datasets_dir / filename

        if not pdf_path.exists():
            print(f"  ⚠️  {filename} — file not found")
            pdf_index = PdfIndex(
                filename=filename, category=category,
                route=mode, total_pages=0, status="error", error="file not found"
            )
            index.add_pdf(pdf_index)
            continue

        print(f"  📄 {filename} ({category})", end="")
        cache = str(datasets_dir.parent / "cache" / "ocr_texts")
        result = detect(str(pdf_path), category=category, mode=mode, cache_dir=cache)

        if result.error:
            print(f"  ❌ {result.error}")
            pdf_index = PdfIndex(
                filename=filename, category=category,
                route=mode, total_pages=result.total_pages,
                status="error", error=result.error,
            )
        else:
            param_pages = [
                PageEntry(
                    page=p.page,
                    signals=[f"{s.tier}:{s.keyword}" for s in p.signals],
                    confidence=p.confidence,
                )
                for p in result.parameter_pages
            ]
            pdf_index = PdfIndex(
                filename=filename, category=category,
                route=mode, total_pages=result.total_pages,
                parameter_pages=param_pages,
            )
            s = pdf_index.summary
            confs = [p.confidence for p in param_pages]
            print(f"  → {s['param_pages']} param pages / {s['total_pages']} total"
                  f" ({s['savings_pct']}% saved)  [{', '.join(confs)}]")

        index.add_pdf(pdf_index)

    # Write output
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    index.to_json(str(output_path))
    print(f"\n📊 Results saved to {output_path}")

    return index


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Parameter Page Locator")
    parser.add_argument("--input", default="datasets",
                        help="Datasets directory (default: datasets/)")
    parser.add_argument("--output", default="output/index.json",
                        help="Output path (default: output/index.json)")
    parser.add_argument("--mode", default="text", choices=["text", "ocr", "auto"],
                        help="Detection mode (default: text)")
    args = parser.parse_args()

    run(datasets_dir=args.input, output_path=args.output, mode=args.mode)
