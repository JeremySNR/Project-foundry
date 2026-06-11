"""Delivery memory: Foundry learning from its own history.

Every finished run is distilled into a ``foundry_run_outcomes`` row (outcomes),
mined into routing priors that feed the catalog enricher (priors), and
aggregated into the ROI metrics behind ``GET /metrics/delivery`` (metrics).
"""
