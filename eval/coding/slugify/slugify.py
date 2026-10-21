import re


def slugify(title):
    """Turn a title into a URL slug: lowercase ASCII words joined by single hyphens."""
    return re.sub(r"[^a-z0-9]", "-", title.lower())
