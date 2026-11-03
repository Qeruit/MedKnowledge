"""Preview local Markdown splitting without writing the database or calling a model."""
import argparse
import json
from pathlib import Path

from app.documents import read_documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--tokens", type=int, default=None)
    parser.add_argument("--overlap", type=int, default=None)
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    chunks = read_documents(args.file, args.file.name, split_options={
        "enabled": False if args.disable else None,
        "chunk_tokens": args.tokens, "overlap_tokens": args.overlap})
    report = {"chunks": len(chunks), "oversized": sum(c.metadata["oversized"] for c in chunks),
              "maxTokens": max((c.metadata["tokenCount"] for c in chunks), default=0)}
    if not args.summary:
        report["items"] = [c.metadata for c in chunks]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
