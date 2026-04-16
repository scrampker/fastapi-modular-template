"""Indent-aware YAML list appender.

Used to add entries to the ansible-vars-style list blocks in:
  playbooks/workloads/nginx-certs.yml       (nginx_certs)
  playbooks/workloads/nginx-vhosts.yml      (nginx_vhosts)
  playbooks/workloads/scottycore-apps.yml   (scottycore_apps)
  inventory/workloads.yml                   (docker_melbourne.hosts)

PyYAML round-tripping would destroy comments + formatting, so we do text-level
inserts anchored to the list key's indentation.
"""

from pathlib import Path
import re


def append_to_yaml_list(path: Path, list_key: str,
                        entry_lines: list[str]) -> bool:
    """Append `entry_lines` to a YAML list named `list_key` in `path`.

    Locates `<indent><list_key>:` (block form) or `<indent><list_key>: []`
    (empty inline form). Walks forward accepting only lines whose indent is
    greater than the list_key's indent (i.e. list body), stopping at the
    first line that dedents to <= list_key indent. Inserts the new entry
    immediately before that exit point.

    `entry_lines` should be pre-indented at the same level as existing list
    items (typically list_key_indent + 2). Trailing newlines are optional.

    Returns True on insert, False if the key couldn't be located.
    """
    lines = path.read_text().splitlines(keepends=True)

    key_idx: int | None = None
    key_indent = 0
    for i, ln in enumerate(lines):
        m = re.match(rf"^(\s*){re.escape(list_key)}:\s*$", ln)
        if m:
            key_idx = i
            key_indent = len(m.group(1))
            break

    if key_idx is None:
        # Inline empty-list form: `<indent><list_key>: []`
        for i, ln in enumerate(lines):
            m = re.match(rf"^(\s*){re.escape(list_key)}:\s*\[\]\s*$", ln)
            if m:
                indent = m.group(1)
                replacement = f"{indent}{list_key}:\n"
                for el in entry_lines:
                    replacement += el if el.endswith("\n") else el + "\n"
                lines[i] = replacement
                path.write_text("".join(lines))
                return True
        return False

    # Scan forward through the list body until we dedent out of it
    exit_idx = len(lines)
    for j in range(key_idx + 1, len(lines)):
        ln = lines[j]
        if ln.strip() == "":
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        if leading <= key_indent:
            exit_idx = j
            break

    # Preserve any trailing blank line separating the block from the next section
    insert_at = exit_idx
    while insert_at > key_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    snippet = "".join(
        el if el.endswith("\n") else el + "\n"
        for el in entry_lines
    )
    lines.insert(insert_at, snippet)
    path.write_text("".join(lines))
    return True
