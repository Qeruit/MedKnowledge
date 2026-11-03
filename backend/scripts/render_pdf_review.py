"""Local visual review sheets, preserving physical page labels and full-page images."""
import argparse
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    evidence = json.loads((args.folder / "page_evidence.json").read_text(encoding="utf-8"))
    # All pages, including neighbours marked auto_ok, are available for context.
    images = args.folder / "images"
    images.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-r", "100", "-png", str(args.source), str(images / "page")], check=True)
    selected = [p["physicalPage"] for p in evidence["pages"] if p["auditStatus"] == "needs_review"]
    for offset in range(0, len(selected), 4):
        group = selected[offset:offset + 4]
        sheet = Image.new("RGB", (1700, 2260), "#dddddd")
        draw = ImageDraw.Draw(sheet)
        for index, page in enumerate(group):
            image = Image.open(images / f"page-{page:02d}.png").convert("RGB")
            image.thumbnail((838, 1090))
            x, y = (index % 2) * 850, (index // 2) * 1130
            draw.text((x + 10, y + 8), f"PHYSICAL PAGE {page}", fill="black")
            sheet.paste(image, (x + (850 - image.width) // 2, y + 30))
        sheet.save(args.folder / f"sheet-{offset // 4 + 1:02d}.png")
    print(f"Rendered {len(evidence['pages'])} pages; {len(selected)} risk pages on {(len(selected)+3)//4} sheets.")


if __name__ == "__main__":
    main()
