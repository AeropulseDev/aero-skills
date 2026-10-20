#!/usr/bin/env python3
"""Собирает установленную копию указателя: SKILL.md + registry.open.md + registry.closed.md -> INDEX.built.md.
Только стандартная библиотека. INDEX.built.md содержит закрытые записи — он в .gitignore."""
import os
from pathlib import Path
here = Path(__file__).resolve().parent
parts = [(here / "SKILL.md").read_text(encoding="utf-8").rstrip() + "\n"]
def closed_path():
    env = os.environ.get("SKILLS_CLOSED_REGISTRY")
    if env:
        return Path(env)
    ptr = here / "registry.closed.path"
    if ptr.exists():
        line = ptr.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if line:
            return Path(line)  # путь как в ОС, где запускается сборка; в облачной VM — задать SKILLS_CLOSED_REGISTRY
    return here / "registry.closed.md"

for p, title in ((here / "registry.open.md", "Реестр: открытые навыки"), (closed_path(), "Реестр: закрытые и командные навыки")):
    if not p.exists():
        continue
    lines = p.read_text(encoding="utf-8").splitlines()
    body = "\n".join(l for l in lines if not l.startswith("# ")).strip()
    parts.append(f"\n# {title}\n\n{body}\n")
out = here / "INDEX.built.md"
out.write_text("\n".join(parts), encoding="utf-8")
print(f"{out} — {out.stat().st_size} байт; закрытый реестр: {closed_path()} ({'есть' if closed_path().exists() else 'НЕ НАЙДЕН'})")
