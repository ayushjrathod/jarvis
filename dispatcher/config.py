"""Load config.yaml into a plain object. Paths resolve relative to the file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: Path = None
    queue_dir: Path = None
    poll_interval_s: float = 5.0
    queue_max_retries: int = 3
    max_concurrent_agentic: int = 2
    claude_bin: str = "claude"
    claude_config_dir: str | None = None
    models: dict = field(default_factory=dict)
    quick_session_idle_minutes: float = 0.0
    budgets: dict = field(default_factory=dict)
    default_tools: list = field(default_factory=list)
    task_types: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    learning: dict = field(default_factory=dict)
    automations: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    computer: dict = field(default_factory=dict)
    inbox: dict = field(default_factory=dict)
    screenshots_dir: Path = None
    stt: dict = field(default_factory=dict)
    embeddings: dict = field(default_factory=dict)
    security: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or os.environ.get("MC_CONFIG", Path(__file__).parent.parent / "config.yaml"))
        raw = yaml.safe_load(path.read_text())
        d = raw.get("dispatcher", {})
        root = path.parent.resolve()

        def rel(p):
            p = Path(p)
            return p if p.is_absolute() else root / p

        cfg = cls(root=root)
        cfg.host = d.get("host", cfg.host)
        cfg.port = d.get("port", cfg.port)
        cfg.db_path = rel(d.get("db", "data/mission.db"))
        cfg.queue_dir = rel(d.get("queue_dir", "queue"))
        cfg.poll_interval_s = d.get("poll_interval_s", cfg.poll_interval_s)
        cfg.queue_max_retries = d.get("queue_max_retries", cfg.queue_max_retries)
        cfg.max_concurrent_agentic = d.get("max_concurrent_agentic", cfg.max_concurrent_agentic)
        cfg.claude_bin = d.get("claude_bin", cfg.claude_bin)
        cfg.claude_config_dir = d.get("claude_config_dir")
        cfg.models = d.get("models", {})
        cfg.quick_session_idle_minutes = d.get("quick_session_idle_minutes", 0.0)
        cfg.budgets = d.get("budgets", {})
        cfg.default_tools = (d.get("task_defaults") or {}).get("allowed_tools", [])
        cfg.task_types = d.get("task_types", {})
        cfg.memory = d.get("memory", {})
        cfg.learning = d.get("learning", {})
        cfg.automations = d.get("automations", {})
        cfg.media = d.get("media", {})
        cfg.computer = d.get("computer", {})
        cfg.inbox = d.get("inbox", {})
        cfg.screenshots_dir = rel(d.get("screenshots_dir", "data/screenshots"))
        cfg.stt = d.get("stt", {})
        cfg.embeddings = d.get("embeddings", {})
        cfg.security = d.get("security", {})
        return cfg

    @property
    def public_hosts(self) -> list[str]:
        """Hostnames of any non-loopback front door the CSRF origin guard should
        trust for same-origin SPA POSTs — e.g. a Tailscale Serve
        `<machine>.<tailnet>.ts.net` name or a LAN IP (security.public_hosts,
        a string or a list). This is ONLY the origin-guard allowlist; it grants
        no authentication. Tailscale keeps the dispatcher on loopback and proxies
        HTTPS in front, so reaching it off-network needs no token middleware and
        opens no public port (scripts/setup_tailscale.sh)."""
        v = self.security.get("public_hosts") or []
        return [v] if isinstance(v, str) else list(v)

    @property
    def privileged_areas(self) -> list[str]:
        """Areas allowed to declare Bash / unscoped Edit-Write in their SKILL.md
        frontmatter (security.privileged_areas). Empty by default — every other
        area has those grants stripped at load time (areas.py)."""
        return self.security.get("privileged_areas") or []

    @property
    def allow_simulate_refusal(self) -> bool:
        """Whether an EXTERNAL caller may set `metadata.simulate_refusal` and so
        force a second run on the fallback model. False by default — it is a
        test seam, and leaving it open let anything reaching POST /task double
        the cost of every agentic run (found 2026-08-08). Turn it on only while
        running scripts/smoke_phase_a.sh."""
        return bool(self.security.get("allow_simulate_refusal"))

    def tools_for(self, task_type: str | None) -> list[str]:
        tt = self.task_types.get(task_type or "", {})
        return tt.get("allowed_tools", self.default_tools)

    # `quick_cost()` and the `prices:` block it read lived here to price the
    # Messages API's token counts by hand. That backend was removed 2026-07-27
    # and `claude -p` reports `total_cost_usd` itself, so both were dead —
    # removed 2026-08-02, same clean-up as `budgets.quick_max_tokens`.
