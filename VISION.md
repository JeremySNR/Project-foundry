# Vision

> This is the canonical product description for Project Foundry. It is the north
> star: read it before touching code. It keeps the wedge narrow and the layering
> honest — every feature should help **turn product intent into governed software
> delivery**, not just add plumbing.

Project Foundry is an AI-native engineering control plane designed to turn
product intent into governed software delivery. Rather than replacing tools such
as Linear, GitHub, Cursor, Claude Code, CodeRabbit, Datadog or Slack, Foundry
sits above them and coordinates the flow of work between them. Its purpose is to
provide the missing orchestration layer that understands what work is being
requested, gathers the right context, applies governance and risk controls, and
then directs AI agents and humans through the delivery process.

The first version of Foundry focuses on a single workflow: taking a Linear ticket
and turning it into a reviewed pull request. When a ticket is submitted for
analysis, Foundry evaluates whether it contains enough information to be
implemented, identifies missing requirements, gathers technical context from
repositories and documentation, classifies risk, and generates a structured
delivery plan. This gives product owners, engineers and QA a shared understanding
of the work before any code is written and ensures that AI agents operate from
verified context rather than assumptions.

Once a plan is approved, Foundry can launch a coding agent such as Cursor Cloud
Agents, Claude Code or another supported provider to perform implementation work.
Foundry does not attempt to be the coding agent itself; instead, it acts as the
coordinator that decides what should happen, when it should happen, and who or
what should perform the task. Every action is governed by policy rules, approval
workflows and audit trails, ensuring that AI-assisted development remains safe,
explainable and aligned with engineering standards.

Over time, Foundry will evolve beyond ticket-to-PR automation into a broader
Engineering OS. The long-term vision is a platform that connects planning,
development, testing, deployment, observability and incident management into a
single intelligent workflow. In that future state, Foundry becomes the control
plane for engineering teams, providing the context, governance and orchestration
required for humans and AI agents to collaborate effectively across the entire
software development lifecycle.

## Principles this implies

These are the tests we apply to any proposed feature:

1. **Intent in, governed delivery out.** The unit of work is product *intent*;
   the deliverable is *governed delivery*, not code. If a change doesn't help
   intent become governed delivery, it's plumbing — justify it as such.
2. **Sit above, never replace.** Foundry owns no editor, repo, or chat. It
   integrates through adapters so existing tools keep their strengths.
3. **Governance is hard rules, not prompts.** Allow/deny decisions live in the
   policy engine and are recorded; the model only *advises* risk.
4. **Verified context, not assumptions.** Context enrichment is a *safety*
   feature. When confidence is low (e.g. which repo), Foundry blocks rather than
   guesses.
5. **Human-in-the-loop is a first-class state.** Approval is an explicit, audited
   stage in the workflow, not an afterthought.
6. **Tool-agnostic at both ends.** Intelligence (LLM/LangGraph) and hands (coding
   agents) are swappable behind stable contracts, so no single vendor's limits
   can sink the platform.
7. **Everything is auditable.** Every decision, artifact and approval is
   content-hashed and explainable after the fact.
