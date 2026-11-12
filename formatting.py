import re
from datetime import datetime
from difflib import SequenceMatcher
from html import escape


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%d.%m.%Y %H:%M:%S %z")


def version_runs(before, after):
    tokens = re.compile(r"\w+|\s+|[^\w\s]")
    old, new = tokens.findall(before), tokens.findall(after)
    old_runs, new_runs = [], []
    for operation, start_old, end_old, start_new, end_new in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if end_old > start_old:
            old_runs.append((None if operation == "equal" else "s", "".join(old[start_old:end_old])))
        if end_new > start_new:
            new_runs.append((None if operation == "equal" else "u", "".join(new[start_new:end_new])))
    return old_runs, new_runs


def notice_parts(title, details, sections, footer):
    lines = [title, *details]
    page = [f"<b>{escape(title, quote=False)}</b>", *(escape(line, quote=False) for line in details)]
    used = sum(len(line) + 1 for line in lines)
    for label, runs in sections:
        for html, size in rendered_runs(runs, 1600):
            if page and used + len(label) + size + 3 > 1900:
                yield "\n".join(page)
                page, used = [], 0
            page.append(f"\n<b>{escape(label, quote=False)}</b>\n<blockquote>{html}</blockquote>")
            used += len(label) + size + 3
    if footer:
        page.append(f"\n{escape(footer, quote=False)}")
    if page:
        yield "\n".join(page)


def diff_parts(before, after, limit=2000):
    tokens = re.compile(r"\w+|\s+|[^\w\s]")
    old, new = tokens.findall(before), tokens.findall(after)
    runs = [(None, "Правка: зачёркнуто удалённое, подчёркнуто добавленное\n\n")]
    for opcode, old_start, old_end, new_start, new_end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if opcode in ("delete", "replace"):
            runs.append(("s", "".join(old[old_start:old_end])))
        if opcode in ("insert", "replace"):
            runs.append(("u", "".join(new[new_start:new_end])))
        if opcode == "equal":
            runs.append((None, "".join(old[old_start:old_end])))

    for html, _ in rendered_runs(runs, limit):
        yield html


def rendered_runs(runs, limit):
    parts, used = [], 0
    for tag, text in runs:
        while text:
            segment, text = text[:limit - used], text[limit - used:]
            rendered = escape(segment, quote=False)
            parts.append(f"<{tag}>{rendered}</{tag}>" if tag else rendered)
            used += len(segment)
            if used == limit:
                yield "".join(parts), used
                parts, used = [], 0
    if parts:
        yield "".join(parts), used
