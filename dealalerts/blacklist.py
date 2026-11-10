"""
Deals you never want emailed, from every store: deal_blacklist.txt.

One word or phrase per line. A deal is skipped when its name, brand or
size contains the phrase as whole words (case and accents don't matter,
and a trailing "s" is allowed, so "sampler" also blocks "Samplers"):

    Gurkha              a brand
    Tatiana Classic     one cigar line
    sampler             a kind of product
    https://...         one product, by its link

Lines starting with # are notes and are ignored.

Standard library only, so every script can use it.
"""

import re
import unicodedata
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "deal_blacklist.txt"


def _plain(text):
    """Lowercase without accents, so 'Estelí' matches 'Esteli'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c)).lower()


def load(path=FILE):
    """[(entry, matcher)] from the file; empty if there is no file."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.lower().startswith(("http://", "https://")):
            url = entry.split("?")[0].rstrip("/").lower()
            out.append((entry, lambda text, u=url: u in text))
        else:
            rx = re.compile(r"(?<!\w)" + re.escape(_plain(entry)) + r"(?:e?s)?(?!\w)")
            out.append((entry, rx.search))
    return out


_entries = None


def blocked(item, entries=None):
    """The blacklist entry that blocks this deal, or None."""
    global _entries
    if entries is None:
        if _entries is None:
            _entries = load()
        entries = _entries
    if not entries:
        return None
    text = _plain(" ".join(str(item.get(k) or "")
                           for k in ("name", "brand", "size", "url")))
    return next((e for e, match in entries if match(text)), None)


def remove_blocked(items, label="deal"):
    """items without blacklisted ones; prints how many were skipped."""
    kept, hits = [], {}
    for i in items:
        e = blocked(i)
        if e:
            hits[e] = hits.get(e, 0) + 1
        else:
            kept.append(i)
    if hits:
        detail = ", ".join(f"{e} ({n})" for e, n in sorted(hits.items()))
        print(f"Blacklist: skipped {sum(hits.values())} {label}(s): {detail}")
    return kept
