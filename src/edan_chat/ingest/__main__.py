"""Reproducible ingestion pipeline:  `uv run python -m edan_chat.ingest [--force]`

download PDF -> parse (geometry) -> normalize -> Parquet/CSV + DuckDB -> validate -> manifest
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from edan_chat import config
from edan_chat.ingest.build_db import build, write_manifest
from edan_chat.ingest.parse_pdf import parse_pdf
from edan_chat.ingest.validate import run_checks


def download(force: bool = False) -> None:
    if config.PDF_PATH.exists() and not force:
        print(f"PDF already present: {config.PDF_PATH}")
        return
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {config.PDF_URL}")
    req = urllib.request.Request(config.PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(config.PDF_PATH, "wb") as f:
        f.write(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-download", action="store_true")
    args = ap.parse_args()

    download(args.force_download)
    print("Parsing PDF ...")
    parsed = parse_pdf(config.PDF_PATH)
    for issue in parsed["issues"]:
        print("  parser note:", issue)
    print(f"  {len(parsed['circonscriptions'])} circonscriptions, {len(parsed['candidates'])} candidatures")
    build(parsed, config.PROCESSED_DIR, config.DB_PATH)
    report = run_checks(config.DB_PATH)
    manifest = write_manifest(config.MANIFEST_PATH, config.PDF_PATH, report)
    ok = all(v["passed"] for v in report.values())
    for name, v in report.items():
        print(f"  [{'OK ' if v['passed'] else 'FAIL'}] {name}" + ("" if v["passed"] else f" {v['failures']}"))
    print(f"Dataset version {manifest['dataset_version']} -> {config.DB_PATH}")
    if not ok:
        print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
