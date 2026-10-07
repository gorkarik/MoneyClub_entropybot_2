"""Change one value in a YAML file's text, keeping comments and layout.

Used by the menu (club.py) for every setting it saves, and by the bot itself
for the midline set by auto-calibration. Plain text edits on purpose: a YAML
round trip would drop the Russian comments the files carry.
"""
from __future__ import annotations

import re


def replace_value(text: str, section: str, key: str, value: str) -> str:
    """Меняет значение section.key, сохраняя комментарии и выравнивание."""
    lines = text.split("\n")
    in_sec = False
    for i, line in enumerate(lines):
        if re.match(r"^[A-Za-z_]\w*\s*:", line):
            in_sec = re.match(rf"^{re.escape(section)}\s*:", line) is not None
            continue
        if not in_sec:
            continue
        m = re.match(rf"^(\s+{re.escape(key)}\s*:\s*)([^#]*?)(\s*#.*)?$", line)
        if not m:
            continue
        new = m.group(1) + value
        comment = (m.group(3) or "").lstrip()
        if comment:
            hash_col = len(line) - len(comment)
            new += " " * max(2, hash_col - len(new)) + comment
        lines[i] = new
        return "\n".join(lines)
    raise KeyError(f"в config.yaml не найден параметр {section}.{key}")


def set_value(text: str, section: str, key: str, value: str) -> str:
    """Как _replace_yaml_value, но если параметра или целой секции в файле
    нет (config.yaml от старой версии) — дописывает их, не трогая остальное."""
    try:
        return replace_value(text, section, key, value)
    except KeyError:
        pass
    lines = text.split("\n")
    head = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}\s*:\s*(#.*)?$", line):
            head = i
            break
    if head is None:
        if lines and lines[-1].strip() == "":
            lines.pop()
        lines += ["", f"{section}:", f"  {key}: {value}", ""]
        return "\n".join(lines)
    # отступ берём у первого параметра секции, если он есть
    indent = "  "
    end = head + 1
    for j in range(head + 1, len(lines)):
        if re.match(r"^[A-Za-z_]\w*\s*:", lines[j]):
            break
        m = re.match(r"^(\s+)[A-Za-z_]\w*\s*:", lines[j])
        if m:
            indent = m.group(1)
            end = j + 1
    lines.insert(end, f"{indent}{key}: {value}")
    return "\n".join(lines)
