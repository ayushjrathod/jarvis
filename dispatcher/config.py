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
    quick_backend: str = "auto"
    quick_session_idle_minutes: float = 0.0
    budgets: dict = field(default_factory=dict)
    default_tools: list = field(default_factory=list)
    task_types: dict = field(default_factory=dict)
    prices: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    learning: dict = field(default_factory=dict)

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
        cfg.quick_backend = d.get("quick_backend", "auto")
        cfg.quick_session_idle_minutes = d.get("quick_session_idle_minutes", 0.0)
        cfg.budgets = d.get("budgets", {})
        cfg.default_tools = (d.get("task_defaults") or {}).get("allowed_tools", [])
        cfg.task_types = d.get("task_types", {})
        cfg.prices = d.get("prices", {})
        cfg.memory = d.get("memory", {})
        cfg.learning = d.get("learning", {})
        return cfg

    def tools_for(self, task_type: str | None) -> list[str]:
        tt = self.task_types.get(task_type or "", {})
        return tt.get("allowed_tools", self.default_tools)

    def quick_cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        p = self.prices.get(model)
        if not p:
            return None
        return input_tokens * p["input"] / 1e6 + output_tokens * p["output"] / 1e6
