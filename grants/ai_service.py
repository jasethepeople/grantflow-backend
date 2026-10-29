"""
AI drafting engine.

Generates a first-draft section for a grant proposal by combining:
  - the organization's mission statement
  - the target funder program's focus areas / program officer notes
  - the grant's other existing sections (for consistency across the proposal)
  - the section's own word limit

into a single prompt sent to the Anthropic API. The result is returned as
plain text; the caller (the view) is responsible for saving it as a new
SectionVersion — this module has no knowledge of version numbers or the DB
transaction, it just drafts text.
"""

from __future__ import annotations

from django.conf import settings

try:
    import anthropic
except ImportError:  # pragma: no cover - anthropic is a hard requirement, but fail soft on import
    anthropic = None


class AIConfigurationError(Exception):
    """Raised when the AI service is called but isn't configured (no API key)."""


class AIGenerationError(Exception):
    """Raised when the underlying API call fails (network, rate limit, bad response, etc.)."""


SECTION_GUIDANCE = {
    "executive_summary": (
        "Write a concise executive summary (2-3 paragraphs) that states the request amount, "
        "the problem, the proposed solution, and the expected impact, in that order."
    ),
    "statement_of_need": (
        "Write a Statement of Need. Establish the problem with specificity, explain why it "
        "matters now, and ground it in the organization's direct experience. Avoid generic "
        "statistics with no source; where you would cite a stat, leave a bracketed placeholder "
        "like [CITE: local unemployment rate] for the writer to fill in with a verified source."
    ),
    "goals_objectives": (
        "Write Goals & Objectives. State 1-2 broad goals, then 3-5 SMART objectives "
        "(specific, measurable, achievable, relevant, time-bound) that ladder up to them."
    ),
    "methodology": (
        "Write the Methodology / Program Design section. Describe the activities in a logical "
        "sequence, who carries each one out, and how they connect to the stated objectives."
    ),
    "evaluation_plan": (
        "Write an Evaluation Plan. Name the specific metrics that will be tracked for each "
        "objective, the data collection method, and the timeline for review."
    ),
    "organizational_capacity": (
        "Write Organizational Capacity. Use the organization's mission statement and describe "
        "the staffing, systems, and track record that make it capable of delivering this project."
    ),
    "sustainability": (
        "Write a Sustainability Plan. Describe how the project continues past the grant period: "
        "future funding sources, partnerships, or the point at which it becomes self-sustaining."
    ),
    "budget_narrative": (
        "Write a Budget Narrative that explains, in prose, why each major cost category is "
        "necessary to achieve the project's objectives. Do not invent specific dollar figures "
        "you have not been given — reference categories in general terms instead."
    ),
    "other": "Write this section in a clear, professional grant-writing style.",
}


def _build_prompt(section) -> tuple[str, str]:
    grant = section.grant
    org = grant.organization
    funder_program = grant.funder_program

    system_prompt = (
        "You are an experienced grant writer helping a nonprofit draft a funding proposal. "
        "Write in a clear, concrete, professional tone. Never fabricate statistics, dates, "
        "named partners, or dollar amounts that were not provided to you — use a bracketed "
        "placeholder like [NEEDS: specific detail] instead. Output only the section text, "
        "no headers, no preamble, no meta-commentary."
    )

    context_lines = [f"Organization: {org.name}"]
    if org.mission_statement:
        context_lines.append(f"Organization mission: {org.mission_statement}")
    context_lines.append(f"Proposal title: {grant.title}")

    if funder_program:
        context_lines.append(f"Target funder: {funder_program.funder.name} — {funder_program.name}")
        if funder_program.funder.focus_areas:
            context_lines.append(f"Funder focus areas: {funder_program.funder.focus_areas}")
        if funder_program.program_officer_notes:
            context_lines.append(f"Program officer notes: {funder_program.program_officer_notes}")
        if funder_program.past_awards_summary:
            context_lines.append(f"Funder's past awards pattern: {funder_program.past_awards_summary}")

    if grant.amount_requested:
        context_lines.append(f"Amount requested: ${grant.amount_requested}")

    other_sections = section.grant.sections.exclude(id=section.id).exclude(
        section_type=section.section_type
    )
    existing_context = []
    for other in other_sections:
        version = other.current_version
        if version and version.content.strip():
            existing_context.append(f"--- {other.title} (existing draft) ---\n{version.content.strip()}")
    if existing_context:
        context_lines.append(
            "For consistency, here is content already drafted elsewhere in this proposal:\n"
            + "\n\n".join(existing_context)
        )

    guidance = SECTION_GUIDANCE.get(section.section_type, SECTION_GUIDANCE["other"])
    context_lines.append(f"\nSection to write: {section.title}")
    context_lines.append(guidance)
    if section.word_limit:
        context_lines.append(f"Target length: no more than {section.word_limit} words.")

    user_prompt = "\n".join(context_lines)
    return system_prompt, user_prompt


def generate_draft(section, extra_instructions: str = "") -> str:
    """
    Generates draft text for a single GrantSection. Raises AIConfigurationError if no
    API key is set, or AIGenerationError if the API call itself fails.
    """
    if anthropic is None:
        raise AIConfigurationError("The 'anthropic' package is not installed.")

    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        raise AIConfigurationError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env to enable AI drafting."
        )

    system_prompt, user_prompt = _build_prompt(section)
    if extra_instructions:
        user_prompt += f"\n\nAdditional instructions from the user: {extra_instructions}"

    client = anthropic.Anthropic(api_key=api_key)
    model = getattr(settings, "AI_DRAFTING_MODEL", "claude-sonnet-4-6")

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as exc:
        raise AIGenerationError(f"AI drafting request failed: {exc}") from exc

    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not text_blocks:
        raise AIGenerationError("AI drafting request returned no text content.")
    return "\n".join(text_blocks).strip()
