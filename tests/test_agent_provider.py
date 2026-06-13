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


# One fake-but-shaped credential per detection pattern. None are real secrets.
_LEAKY_VALUES = {
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----",
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "github_token": "ghp_0123456789abcdefghij0123",
    "slack_token": "xoxb-123456789012-abcdefghijkl",
    "google_api_key": "AIza" + "a" * 35,
    "stripe_key": "sk_live_0123456789abcdef0123",
    "openai_key": "sk-proj-0123456789abcdefghijABCD",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "basic_auth_url": "Clone https://user:p4ssw0rd@example.com/repo.git first.",
    "labelled_authorization": 'Set header authorization: Bearer abcdef1234567890XYZ',
    "bare_bearer": "Send a bearer abcdef1234567890XYZ header.",
}


@pytest.mark.parametrize("name,value", sorted(_LEAKY_VALUES.items()))
def test_secret_patterns_are_rejected(name: str, value: str) -> None:
    provider = ManualProvider()
    leaky = _job_input(agent_instructions=f"Implement favourites. {value}")
    with pytest.raises(SecretLeakError):
        provider.create_job(leaky)


def test_secret_in_ticket_url_is_rejected() -> None:
    """A token in a ticket URL query string must be caught (issue #16)."""
    provider = ManualProvider()
    leaky = _job_input(
        ticket_url="https://linear.app/x/issue/LIN-123?token=SuperSecretValue1234"
    )
    with pytest.raises(SecretLeakError):
        provider.create_job(leaky)


def test_secret_in_constraints_is_rejected() -> None:
    """A secret hiding in a constraints list must be caught (issue #16)."""
    provider = ManualProvider()
    leaky = _job_input(
        constraints={"required_tests": ["pytest", "ghp_0123456789abcdefghij0123"]}
    )
    with pytest.raises(SecretLeakError):
        provider.create_job(leaky)


def test_clean_job_input_passes_scan() -> None:
    """Ordinary inputs (slugs, repo names, plain URLs) must not false-positive."""
    provider = ManualProvider()
    clean = _job_input(
        repo="task-management-service",
        branch_name="foundry/lin-99-sk-checkout-refactor",
        ticket_url="https://linear.app/x/issue/LIN-99",
        agent_instructions="Refactor the checkout flow; run pytest and ruff.",
    )
    job = provider.create_job(clean)  # must not raise
    assert job.job_id


def test_forbidden_globs_carried_in_constraints() -> None:
    job_input = _job_input()
    assert "infra/**" in job_input.constraints.do_not_modify
    assert job_input.constraints.allow_new_dependencies is False


def test_constraints_default_max_files() -> None:
    assert JobConstraints().max_files_changed == 12


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        get_provider("does-not-exist")
