"""A human name → a stable identifier.

It lives in the domain rather than in the import module: a recipe or product name is
turned into a slug both when importing from files and when the owner dictates a new
recipe. Two implementations would produce two different slugs for one name, and the
same vegetable would become two records.
"""

from __future__ import annotations

import re

TRANSLIT = str.maketrans(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    "abvgdeejziyklmnoprstufhccss_y_eua",
)


def slugify(name: str) -> str:
    slug = name.lower().translate(TRANSLIT)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug[:100] or "item"


# Words present in almost every name that distinguish nothing
NOISE_WORDS = frozenset({
    "масло", "соус", "домашний", "домашняя", "домашнее", "свежий", "свежая",
    "свежемолотый", "молотая", "молотый", "тёртый", "рубленая", "целиком",
    "крупные", "крупный", "сладкая", "белый", "красный", "для", "или", "и",
})

MIN_STEM = 5  # shorter than this collides by accident too easily


def _stems(name: str) -> set[str]:
    """The stems of the meaningful words in a name.

    Russian names decline and reorder: the phrase for "chicken breast" in a recipe and
    "chicken fillet" in the pantry are one product, and a character-by-character
    comparison cannot see it. Compare by word beginnings instead: the stem matches.
    """
    stems = set()
    for raw in re.split(r"[\s,()/«»\"'.-]+", name.lower()):
        word = raw.strip()
        if not word or word in NOISE_WORDS:
            continue
        if len(word) <= 4:
            # A short word distinguishes only on a full match: "ghee", "cheese"
            stems.add(word)
        else:
            stems.add(word[:MIN_STEM])
    return stems


def names_match(one: str, other: str) -> bool:
    """Is this the same thing, in a human's judgement. Doubtful cases get an alias."""
    left, right = _stems(one), _stems(other)
    if not left or not right:
        return False
    return bool(left & right)


def match_score(one: str, other: str) -> float:
    """How close two names are: the share of common stems. Used to pick the best."""
    left, right = _stems(one), _stems(other)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))
