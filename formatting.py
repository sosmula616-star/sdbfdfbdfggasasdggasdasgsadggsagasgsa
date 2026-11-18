import re
from datetime import datetime
from difflib import SequenceMatcher
from html import escape


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%d.%m.%Y %H:%M:%S %z")


def author_username(snapshot):
    username = snapshot.get("author_username")
    if not username:
        match = re.search(r"(?:^@|\(@)([A-Za-z0-9_]+)\)?$", snapshot.get("author", ""))
        username = match[1] if match else None
    return username if username and re.fullmatch(r"[A-Za-z0-9_]+", username) else None


def author_link(snapshot, fallback):
    snapshot = snapshot or {}
    label = escape(snapshot.get("author") or fallback, quote=False)
    username = author_username(snapshot)
    if username:
        return f'<a href="https://t.me/{username}">{label}</a>'
    user_id = snapshot.get("author_id")
    if isinstance(user_id, int) and user_id > 0:
        return f'<a href="tg://user?id={user_id}">{label}</a>'
    return label


def media_author(snapshot, fallback):
    username = author_username(snapshot)
    return f"@{username}" if username else snapshot.get("author") or fallback


def notice_parts(header, sections, bot_username):
    footer = escape(f"@{bot_username}", quote=False) if bot_username else ""
    overhead = len(header) + len(footer) + 8
    page, used = [], overhead
    for label, text in sections:
        for html, size in rendered_runs([(None, text)], 1500):
            if page and used + len(label) + size + 4 > 1900:
                yield "\n\n".join([header, *page, footer]).rstrip()
                page, used = [], overhead
            heading = f"{escape(label)}:\n" if label else ""
            page.append(f"{heading}<blockquote>{html}</blockquote>")
            used += len(label) + size + 4
    if page:
        yield "\n\n".join([header, *page, footer]).rstrip()


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
