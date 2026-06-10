"""Repo catalog: sync GitHub org metadata into the DB for context enrichment.

The catalog is a self-maintaining index of per-repo metadata (description,
topics, README head, recent PR titles, top contributors) fetched from the
GitHub API.  The ``CatalogContextEnricher`` scores incoming tickets against
this index instead of a hand-maintained keyword list.
"""

from __future__ import annotations
