"""Render the agent's Markdown report as a single self-contained HTML file (no external requests).

Usage: python render.py report.md [report.html]
"""
import datetime as dt
import html
import re
import sys

import markdown

CSS = """
:root { --bg:#fff; --fg:#1f2933; --muted:#52606d; --line:#d9e2ec; --head:#f0f4f8; --accent:#0b6bcb; --code:#f0f4f8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111827; --fg:#e5e7eb; --muted:#9ca3af; --line:#374151; --head:#1f2937; --accent:#60a5fa; --code:#1f2937; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { width: 100%; max-width: none; margin: 0; padding: 24px 24px 64px; }
header { border-bottom: 2px solid var(--accent); margin-bottom: 24px; padding-bottom: 12px; }
header h1 { margin: 0 0 4px; font-size: 1.6rem; }
header p { margin: 0; color: var(--muted); font-size: .9rem; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.3rem; margin-top: 2rem; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
h3 { font-size: 1.1rem; margin-top: 1.5rem; }
a { color: var(--accent); }
.table-wrap { overflow-x: auto; margin: 12px 0; }
table { border-collapse: collapse; width: 100%; min-width: 100%; font-size: .92rem; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: var(--head); }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; font-size: .88em; word-break: break-all; }
li { margin: 4px 0; }
@media print { body { background:#fff; color:#000; } main { max-width:none; } }
"""


LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")


def normalize_markdown(md_text: str) -> str:
    """Model output is GitHub-style Markdown; Python-Markdown is stricter. Without this, a list or table that
    follows a line of text with no blank line renders as literal '- ' text, and 2-space nested bullets flatten."""
    out, in_fence, prev_kind = [], False, "blank"
    for line in md_text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            prev_kind = "code"
            continue
        if in_fence:
            out.append(line)
            continue
        item = LIST_ITEM.match(line)
        if not line.strip():
            kind = "blank"
        elif item:
            kind = "list"
            depth = len(item.group(1).replace("\t", "    ")) // 2  # 2-space nesting -> 4-space
            line = "    " * depth + line.lstrip()
        elif line.lstrip().startswith("|"):
            kind = "table"
        elif prev_kind == "list" and line.startswith((" ", "\t")):
            kind = "list"  # indented continuation of a list item
        else:
            kind = "text"
        starts_block = (kind == "list" and prev_kind in ("text", "table")) or \
                       (kind == "table" and prev_kind in ("text", "list")) or \
                       (kind == "text" and prev_kind == "table")
        if starts_block:
            out.append("")
        out.append(line)
        prev_kind = kind
    return "\n".join(out)


def render(md_text: str, account_hint: str = "") -> str:
    body = markdown.markdown(normalize_markdown(md_text), extensions=["tables", "sane_lists", "fenced_code", "nl2br"])
    body = re.sub(r"<table>", '<div class="table-wrap"><table>', body)
    body = body.replace("</table>", "</table></div>")
    m = re.search(r"^#\s+(.+)$", md_text, re.M)
    title = m.group(1).strip() if m else "CostPilot report"
    if m:  # the page header carries the title, drop the duplicate h1
        body = re.sub(r"<h1>.*?</h1>", "", body, count=1, flags=re.S)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><main>
<header><h1>{html.escape(title)}</h1><p>CostPilot &middot; generated {stamp} &middot; read-only analysis</p></header>
{body}
</main></body></html>
"""


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src.rsplit(".", 1)[0] + ".html"
    with open(src, encoding="utf-8") as f:
        page = render(f.read())
    with open(dst, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {dst}")
