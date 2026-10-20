#!/usr/bin/env python3
"""Собирает установленную копию указателя: SKILL.md + registry.open.md + registry.closed.md -> INDEX.built.md.
Только стандартная библиотека. INDEX.built.md содержит закрытые записи — он в .gitignore."""
from pathlib import Path
here = Path(__file__).resolve().parent
parts = [(here / "SKILL.md").read_text(encoding="utf-8").rstrip() + "\n"]
for name, title in (("registry.open.md", "Реестр: открытые навыки"), ("registry.closed.md", "Реестр: закрытые и командные навыки")):
    p = here / name
    if not p.exists():
        continue
    lines = p.read_text(encoding="utf-8").splitlines()
    body = "\n".join(l for l in lines if not l.startswith("# ")).strip()
    parts.append(f"\n# {title}\n\n{body}\n")
out = here / "INDEX.built.md"
out.write_text("\n".join(parts), encoding="utf-8")
print(f"{out} — {out.stat().st_size} байт; закрытый реестр: {'да' if (here / 'registry.closed.md').exists() else 'нет'}")
