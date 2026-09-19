"""URL slug helper (deliberately incomplete — Genius Dev's debug demo fixes it)."""


def slugify(text):
    return text.lower().replace(" ", "-")
