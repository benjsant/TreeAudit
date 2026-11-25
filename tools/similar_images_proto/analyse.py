"""Analysis of the similar images prototype (not shipped).

Reads the JSON files written by ``run.cjs hash <engine> own+thumb``: for each
image, a gradient hash (256 bits, hex) and a 32 x 32 grey thumbnail (hex).
Two images are "similar" when their hashes differ by at most MAX_BITS bits AND
their thumbnails correlate at MIN_CORRELATION or more. The second test rejects
photos of different screens or documents, which the coarse hash confuses.

    python analyse.py variants <out dir>        test images: chromium-src.json, firefox-src.json
    python analyse.py pairs <file.json> <pairs>  candidate pairs of a real folder, for review
"""

from __future__ import annotations

import itertools
import json
import math
import os
import sys
from collections import defaultdict

MAX_BITS = 30
"""Czkawka suggests about 20 for a 16 x 16 hash; on real photos a second shot of
the same scene reached 25, and the correlation keeps different pictures out."""
MIN_CORRELATION = 0.95
SHOWN_BITS = 36
"""Pairs listed for review: a little beyond MAX_BITS, to see what is left out."""


def bits(a: str, b: str) -> int:
    """Number of differing bits between two hex hashes."""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def correlation(a: str, b: str) -> float:
    """Pearson correlation of two grey thumbnails: insensitive to brightness and contrast."""
    x, y = bytes.fromhex(a), bytes.fromhex(b)
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    dev_x = math.sqrt(sum((v - mean_x) ** 2 for v in x))
    dev_y = math.sqrt(sum((v - mean_y) ** 2 for v in y))
    if not dev_x or not dev_y:
        return 0.0
    return sum((p - mean_x) * (q - mean_y) for p, q in zip(x, y, strict=True)) / (dev_x * dev_y)


def similar(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """Both tests of the module docstring."""
    return bits(a[0], b[0]) <= MAX_BITS and correlation(a[1], b[1]) >= MIN_CORRELATION


def load(path: str) -> dict[str, tuple[str, str]]:
    """File path -> (hash, thumbnail)."""
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)["rows"]
    result = {}
    for row in rows:
        hash_hex, thumb = row["own+thumb"].split(":")
        result[row["file"]] = (hash_hex, thumb)
    return result


def variants(out: str) -> None:
    """Same bits in both browsers; which variants are kept; false pairs among sources."""
    chromium = {os.path.basename(k): v for k, v in load(f"{out}/chromium-src.json").items()}
    firefox = {os.path.basename(k): v for k, v in load(f"{out}/firefox-src.json").items()}
    same = sum(chromium[k] == firefox[k] for k in chromium)
    print(f"chromium = firefox (empreinte et vignette) : {same}/{len(chromium)}")
    sources = sorted(k for k in chromium if "__" not in k)
    by_kind: dict[str, list[tuple[int, float, str]]] = defaultdict(list)
    for name, value in chromium.items():
        if "__" not in name:
            continue
        base, kind = name.split("__")
        source = next(s for s in sources if s.rsplit(".", 1)[0] == base)
        found = (bits(chromium[source][0], value[0]), correlation(chromium[source][1], value[1]))
        by_kind[kind.split(".")[0]].append((*found, base))
    print("variante     écart max  corrélation min  gardées")
    for kind, values in sorted(by_kind.items()):
        kept = sum(d <= MAX_BITS and r >= MIN_CORRELATION for d, r, _ in values)
        worst = min(values, key=lambda v: v[1])
        print(
            f"{kind:12} {max(v[0] for v in values):9}  {worst[1]:15.3f}  "
            f"{kept}/{len(values)}  (la plus basse : {worst[2]})"
        )
    pairs = [
        (bits(chromium[a][0], chromium[b][0]), correlation(chromium[a][1], chromium[b][1]), a, b)
        for a, b in itertools.combinations(sources, 2)
    ]
    first = sorted(p for p in pairs if p[0] <= MAX_BITS)
    both = [p for p in first if p[1] >= MIN_CORRELATION]
    print(f"sources différentes : {len(pairs)} paires, {len(first)} passent l'empreinte, ", end="")
    print(f"{len(both)} passent les deux tests")
    for d, r, a, b in first:
        print(f"   écart {d:2}  corrélation {r:.3f}  {a} ~ {b}")


def pairs(path: str, target: str) -> None:
    """Every pair within SHOWN_BITS, with both measures, written for the contact sheets."""
    images = load(path)
    found = sorted(
        (bits(images[a][0], images[b][0]), correlation(images[a][1], images[b][1]), a, b)
        for a, b in itertools.combinations(sorted(images), 2)
        if bits(images[a][0], images[b][0]) <= SHOWN_BITS
    )
    sheet = []
    for number, (d, r, a, b) in enumerate(found, 1):
        verdict = "RETENUE" if d <= MAX_BITS and r >= MIN_CORRELATION else "écartée"
        label = f"{number}. écart {d}, corrélation {r:.3f} : {verdict}"
        print(f"{label}  {os.path.basename(a)} ~ {os.path.basename(b)}")
        sheet.append([a, b, label])
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(sheet, handle)


if __name__ == "__main__":
    if sys.argv[1] == "variants":
        variants(sys.argv[2])
    else:
        pairs(sys.argv[2], sys.argv[3])
