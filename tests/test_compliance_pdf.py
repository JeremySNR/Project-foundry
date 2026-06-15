"""PDF rendering of compliance evidence packs (issue #36).

The PDF renderers build from the *same* evidence-pack dicts the JSON/HTML
exports use, so these tests assert the rendered bytes are a structurally valid
PDF (header / trailer / non-trivial length) rather than re-checking pack
contents (that lives in ``test_compliance.py``). ``fpdf2`` is the optional
``[pdf]`` extra; the whole module ``importorskip``s it so the offline core suite
stays green where the extra is absent, exactly like the ``cryptography`` tests.

The missing-extra path is covered separately by stubbing the import out, so the
fail-loud behaviour is asserted even though CI has the extra installed.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("fpdf")

from foundry.audit import build_artifact, build_audit_event  # noqa: E402
from foundry.compliance import (  # noqa: E402
    DEFAULT_CONTROL_MAPPINGS,
    PdfRenderingUnavailable,
    build_epic_evidence_pack,
    build_evidence_archive,
    build_evidence_pack,
    render_archive_pdf,
    render_epic_evidence_pdf,
    render_evidence_pdf,
)
from foundry.db import create_all, make_engine, make_session_factory  # noqa: E402
from foundry.db.models import (  # noqa: E402
    ArtifactType,
    AuditEventType,
    FoundryRun,
)
from foundry.schemas.common import RunStatus  # noqa: E402


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    return make_session_factory(engine)


def _seed_run(
    session_factory,
    run_id: str,
    *,
    parent_run_id: str | None = None,
    title: str = "Add favourites",
) -> None:
    with session_factory() as session:
        session.add(
            FoundryRun(
                id=run_id,
                parent_run_id=parent_run_id,
                linear_issue_id=f"issue-{run_id}",
                linear_issue_key=f"ENG-{run_id}",
                status=RunStatus.COMPLETE,
                trigger_type="label",
                approved_by="lead@example.com",
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.TICKET_SNAPSHOT,
                content={"title": title, "body": "AC: a button"},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.DELIVERY_PLAN,
                content={"steps": ["Satisfy acceptance criterion: a button"]},
            )
        )
        session.add(
            build_artifact(
                run_id=run_id,
                artifact_type=ArtifactType.APPROVAL_RECORD,
                content={"user": "lead@example.com", "granted_roles": ["engineering"]},
            )
        )
        session.add(
            build_audit_event(
                run_id=run_id,
                event_type=AuditEventType.RUN_STARTED,
                actor_type="system",
            )
        )
        session.add(
            build_audit_event(
                run_id=run_id,
                event_type=AuditEventType.APPROVAL_GRANTED,
                actor_type="human",
                actor_id="lead@example.com",
                output_content={"user": "lead@example.com"},
            )
        )
        session.commit()


def _assert_valid_pdf(data: bytes) -> None:
    assert isinstance(data, bytes)
    assert data.startswith(b"%PDF-")
    assert b"%%EOF" in data
    # A real document with text/tables is comfortably over a kilobyte; this
    # guards against an "empty PDF" regression.
    assert len(data) > 800


def _evidence_pack(session_factory, run_id):
    with session_factory() as session:
        run = session.get(FoundryRun, run_id)
        return build_evidence_pack(
            session, run, control_mappings=DEFAULT_CONTROL_MAPPINGS
        )


def test_render_evidence_pdf_is_valid_pdf(session_factory) -> None:
    _seed_run(session_factory, "ev1")
    pack = _evidence_pack(session_factory, "ev1")
    _assert_valid_pdf(render_evidence_pdf(pack))


def test_render_evidence_pdf_handles_failed_integrity(session_factory) -> None:
    """A pack that fails its integrity check still renders a valid PDF."""
    _seed_run(session_factory, "ev2")
    pack = _evidence_pack(session_factory, "ev2")
    pack["integrity"]["verified"] = False  # force the FAILED banner branch
    _assert_valid_pdf(render_evidence_pdf(pack))


def test_render_evidence_pdf_handles_unicode(session_factory) -> None:
    """Non-latin-1 ticket text must not crash the core-font renderer."""
    _seed_run(session_factory, "ev3", title="Café ünïcode 你好 🎉 migration")
    pack = _evidence_pack(session_factory, "ev3")
    _assert_valid_pdf(render_evidence_pdf(pack))


def test_render_archive_pdf_is_valid_pdf(session_factory) -> None:
    _seed_run(session_factory, "ar1")
    _seed_run(session_factory, "ar2")
    with session_factory() as session:
        archive = build_evidence_archive(
            session, control_mappings=DEFAULT_CONTROL_MAPPINGS
        )
    assert archive["run_count"] == 2
    _assert_valid_pdf(render_archive_pdf(archive))


def test_render_archive_pdf_handles_empty_range(session_factory) -> None:
    """An archive with no runs still renders a valid (NO RUNS) PDF."""
    with session_factory() as session:
        archive = build_evidence_archive(
            session, control_mappings=DEFAULT_CONTROL_MAPPINGS
        )
    assert archive["run_count"] == 0
    _assert_valid_pdf(render_archive_pdf(archive))


def test_render_epic_evidence_pdf_is_valid_pdf(session_factory) -> None:
    _seed_run(session_factory, "epic-root")
    _seed_run(session_factory, "child-1", parent_run_id="epic-root")
    _seed_run(session_factory, "child-2", parent_run_id="epic-root")
    with session_factory() as session:
        root = session.get(FoundryRun, "epic-root")
        children = (
            session.query(FoundryRun)
            .filter(FoundryRun.parent_run_id == "epic-root")
            .order_by(FoundryRun.created_at, FoundryRun.id)
            .all()
        )
        pack = build_epic_evidence_pack(
            session, root, children, control_mappings=DEFAULT_CONTROL_MAPPINGS
        )
    assert pack["epic"]["rollup"]["status"] == "complete"
    _assert_valid_pdf(render_epic_evidence_pdf(pack))


def test_pdf_unavailable_raises_with_install_hint(session_factory, monkeypatch) -> None:
    """When the [pdf] extra is absent, rendering fails loud with a hint.

    Stubbing ``sys.modules['fpdf'] = None`` makes ``from fpdf import FPDF`` raise
    ``ImportError`` regardless of whether the extra is actually installed, so the
    missing-extra behaviour is asserted even in a CI image that ships it.
    """
    _seed_run(session_factory, "ev-missing")
    pack = _evidence_pack(session_factory, "ev-missing")
    monkeypatch.setitem(sys.modules, "fpdf", None)
    with pytest.raises(PdfRenderingUnavailable) as exc:
        render_evidence_pdf(pack)
    assert "project-foundry[pdf]" in str(exc.value)
