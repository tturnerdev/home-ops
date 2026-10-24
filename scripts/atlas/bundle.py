#!/usr/bin/env python3
"""Bundle the Atlas page with a snapshot of its data for publishing as a
Claude artifact (or any single-file copy): the page renders complete without
fetching anything, and Refresh still pulls newer data from main.

Usage: python3 scripts/atlas/bundle.py [--out <file>]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "kubernetes/apps/infra/atlas/app/site"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "atlas-bundled.html"))
    ap.add_argument("--unwrap", action="store_true", help="strip the document wrapper (for the Claude artifact host)")
    args = ap.parse_args()
    html = (SITE / "index.html").read_text(encoding="utf-8")
    data = {"state": json.loads((SITE / "state.json").read_text(encoding="utf-8"))}
    live = SITE / "live.json"
    if live.exists():
        data["live"] = json.loads(live.read_text(encoding="utf-8"))
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    marker = '<script type="application/json" id="atlas-embedded">{}</script>'
    if marker not in html:
        raise SystemExit("embedded-data marker not found in index.html")
    html = html.replace(marker, '<script type="application/json" id="atlas-embedded">' + blob + "</script>")
    if args.unwrap:
        # The artifact host wraps the page in its own document skeleton: hand it
        # the content only (title, metas and style first), not a full document.
        for tag in ("<!doctype html>", "<html lang=\"en\">", "<head>", "</head>", "<body>", "</body>", "</html>",
                    '<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'):
            html = html.replace(tag + "\n", "", 1).replace(tag, "", 1)
    out = Path(args.out)
    out.write_text(html.lstrip("\n"), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
