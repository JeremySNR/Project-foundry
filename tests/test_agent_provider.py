"""Coding-agent provider contract and security tests."""

from __future__ import annotations

import pytest

from foundry.agents import (
    InMemoryFakeProvider,
    ManualProvider,
    SecretLeakError,
    available_providers,
    get_provider,
)
from foundry.schemas.agent import CodingAgentJobInput, JobConstraints
from foundry.schemas.common import AgentJobStatus


def _job_input(**overrides) -> CodingAgentJobInput:
    base = {
        "run_id": "run-1",
        "repo": "customer-web",
        "branch_name": "foundry/lin-123-customer-favourites",
        "ticket_url": "https://linear.app/x/issue/LIN-123",
        "delivery_plan": {"goal": "Add favourites"},
        "agent_instructions": "Implement favourites per the plan.",
        "constraints": {"do_not_modify": ["infra/**", "migrations/**"]},
    }
    base.update(overrides)
    return CodingAgentJobInput.model_validate(base)


@pytest.mark.parametrize("provider_name", ["manual", "fake"])
def test_every_provider_accepts_same_input(provider_name: str) -> None:
    provider = get_provider(provider_name)
    job = provider.create_job(_job_input())
    assert job.job_id
    assert job.provider == provider_name


def test_registry_lists_known_providers() -> None:
    assert {"manual", "fake"} <= set(available_providers())


def test_manual_provider_creates_job_without_writing() -> None:
    provider = ManualProvider()
    job = provider.create_job(_job_input())
    status = provider.get_job_status(job.job_id)
    assert status.status is AgentJobStatus.CREATED
    assert status.pr_url is None  # manual provider never opens a PR itself


def test_fake_provider_simulates_branch_and_pr() -> None:
    provider = InMemoryFakeProvider()
    job = provider.create_job(_job_input())
    final = provider.run(job.job_id)
    assert final.status is AgentJobStatus.SUCCEEDED
    assert final.branch == "foundry/lin-123-customer-favourites"
    assert final.pr_url and final.pr_url.endswith("/pull/1")


def test_fake_provider_failure_marks_failed() -> None:
    provider = InMemoryFakeProvider(fail=True)
    job = provider.create_job(_job_input())
    final = provider.run(job.job_id)
    assert final.status is AgentJobStatus.FAILED
    assert final.error


def test_secret_in_instructions_is_rejected() -> None:
    provider = ManualProvider()
    leaky = _job_input(
        agent_instructions="Use api_key=SuperSecretValue1234 to authenticate."
    )
    with pytest.raises(SecretLeakError):
        provider.create_job(leaky)


def test_private_key_in_plan_is_rejected() -> None:
    provider = InMemoryFakeProvider()
    leaky = _job_input(
        delivery_plan={
            "goal": "Add favourites",
            "open_questions": [
                "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----"
            ],
        }
    )
    with pytest.raises(SecretLeakError):
        provider.create_job(leaky)


def test_forbidden_globs_carried_in_constraints() -> None:
    job_input = _job_input()
    assert "infra/**" in job_input.constraints.do_not_modify
    assert job_input.constraints.allow_new_dependencies is False


def test_constraints_default_max_files() -> None:
    assert JobConstraints().max_files_changed == 12


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        get_provider("does-not-exist")
