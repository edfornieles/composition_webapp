"""
If migration 0017_monologue_visual_cache was not applied yet, the ORM must not SELECT
unknown columns. We defer those fields until the table has them.
"""

from __future__ import annotations

from django.db import connection

_VISUAL_COLS_OK: bool | None = None


def monologue_visual_columns_ready() -> bool:
    """True when cached_visual_urls and visual_cache_updated_at exist on the table."""
    global _VISUAL_COLS_OK
    if _VISUAL_COLS_OK is not None:
        return _VISUAL_COLS_OK

    from .models import MonologuePersona

    table = MonologuePersona._meta.db_table
    try:
        with connection.cursor() as cursor:
            desc = connection.introspection.get_table_description(cursor, table)
        names = set()
        for col in desc:
            n = getattr(col, "name", None)
            if n is None and col is not None:
                n = col[0]
            if isinstance(n, bytes):
                n = n.decode()
            if n:
                names.add(n)
    except Exception:
        _VISUAL_COLS_OK = False
        return False

    _VISUAL_COLS_OK = "cached_visual_urls" in names and "visual_cache_updated_at" in names
    return _VISUAL_COLS_OK


def monologue_persona_queryset():
    """Use for reads when the DB might be behind migration 0017."""
    from .models import MonologuePersona

    qs = MonologuePersona.objects.all()
    if not monologue_visual_columns_ready():
        qs = qs.defer("cached_visual_urls", "visual_cache_updated_at")
    return qs
