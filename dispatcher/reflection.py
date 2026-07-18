"""Post-task reflection (Phase G): the closed learning loop.

A complex agentic run (num_turns >= threshold) gets a follow-up low-cost run
that RESUMES the same CLI session (warm prompt cache — the same mechanism as
quick-path continuity) with skill-editing tools only, and decides whether the
work taught anything worth persisting into an area skill.

Review discipline adapted from hermes-agent's background_review.py prompts
(MIT, references/hermes-agent): the signals list, the patch-before-create
preference order, and the anti-capture rules. Their skill_manage tool maps to
Claude Code's own Edit/Write scoped to areas/**.
"""

from __future__ import annotations

REFLECTION_TOOLS = ["Read", "Glob", "Grep", "Edit(areas/**)"]

# Task types that must never trigger another reflection (meta-work), on top
# of the source check ("reflection" tasks are themselves submitted with
# source="reflection").
NO_REFLECT_TASK_TYPES = {"reflection", "memory-consolidate", "learn"}

PROMPT = """\
The task above is finished and delivered. You are now in a private review \
pass: decide whether this run produced reusable learning, and persist it.

Skills here are life-area directories under `areas/`: each has a SKILL.md \
with YAML frontmatter (`name`, `description`, `triggers` — spoken phrases \
matched as in-order word subsequences that route future requests here, \
optional `quick_triggers`, `allowed_tools`) and a markdown body of \
instructions; optional support files live in the area dir. The SKILL.md \
body is injected as context whenever the area matches.

Signals that warrant saving something (any one suffices):
- A non-trivial technique, fix, or workaround emerged that would help next time.
- The task's instructions (an area SKILL.md used this run) turned out wrong, \
outdated, or incomplete — patch it NOW.
- A multi-step workflow was figured out that a future run would otherwise \
re-derive.

Preference order — pick the FIRST that fits:
1. Patch the area SKILL.md (or its CLAUDE.md) that guided this run.
2. Add a support file under that area (`references/<topic>.md` for detail, \
`scripts/` for rerunnable steps) plus a one-line pointer in its SKILL.md.
3. Only then create a NEW area directory — and its name must be class-level \
(a category of recurring work, never one task's codename), with frontmatter \
in the format above and realistic spoken `triggers`.

Do NOT save:
- Environment-dependent failures (missing binary, credentials, permissions).
- Blanket negative claims ("X doesn't work") — if something failed on setup \
state, record the fix, never the verdict.
- Transient errors that a retry resolved, or one-off task narratives.
- Anything secret (tokens, keys, personal identifiers).

Hard rules: never edit a file you have not read in THIS review pass; keep \
every edit small and surgical; do not touch anything outside `areas/`.

"Nothing to save." is a legitimate outcome — if so, reply exactly that. \
Otherwise reply with 1-2 sentences on what you patched or created.
"""


def should_reflect(learning_cfg: dict, task: dict, task_meta: dict,
                   result: dict) -> bool:
    """Policy gate for queueing a reflection after a settled agentic run.
    Pure function so the trigger logic stays unit-testable."""
    if not learning_cfg.get("reflection", True):
        return False
    if task.get("source") == "reflection":
        return False
    if task_meta.get("task_type") in NO_REFLECT_TASK_TYPES:
        return False
    if result.get("status") != "done" or not result.get("session_id"):
        return False
    turns = result.get("num_turns") or 0
    return turns >= learning_cfg.get("reflection_min_turns", 12)
