"""Drop-a-markdown-file task queue. Files in queue/*.md with optional YAML
frontmatter (mode, area, ...) become tasks; processed files move to
queue/.processed/<name>.<task_id>.md, unparseable ones to queue/.failed/.
"""

from __future__ import annotations

import asyncio
import logging

import yaml

log = logging.getLogger("dispatcher.queue")


def parse_task_file(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, parts[2].strip()
    return {}, text.strip()


async def watch(service):
    cfg = service.cfg
    qdir = cfg.queue_dir
    processed = qdir / ".processed"
    failed = qdir / ".failed"
    for d in (qdir, processed, failed):
        d.mkdir(parents=True, exist_ok=True)

    while True:
        for f in sorted(qdir.glob("*.md")):
            try:
                meta, body = parse_task_file(f.read_text())
                if not body:
                    raise ValueError("empty task body")
                task = await service.submit(
                    text=body,
                    source="queue",
                    mode=meta.get("mode", "auto"),
                    area=meta.get("area"),
                    metadata=meta or None,
                )
                f.rename(processed / f"{f.stem}.{task['id']}.md")
                log.info("queued %s as task %s (%s)", f.name, task["id"], task["kind"])
            except Exception:
                log.exception("failed to ingest %s", f.name)
                try:
                    f.rename(failed / f.name)
                except OSError:
                    pass
        await asyncio.sleep(cfg.poll_interval_s)
