"""Render .md as HTML email body + attach raw .md, send via Resend."""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone

import resend


def _inline_md(text: str) -> str:
    """Convert inline markdown (links, bold, italic) to HTML."""
    # Links first: [text](url)
    text = re.sub(
        r'\[([^\]]+)\]\(([^)]+)\)',
        r'<a href="\2" style="color:#2563eb;text-decoration:underline">\1</a>',
        text,
    )
    # Bold: **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic: *text* (not preceded/followed by *)
    text = re.sub(r'(?<!\*)\*(.+?)\*(?!\*)', r'<em>\1</em>', text)
    return text


def _md_to_html(md: str) -> str:
    """
    Convert markdown to email-safe HTML.
    Single-column, inline styles, max-width 600px. Outlook-safe.
    """
    lines = md.split("\n")
    out: list[str] = []
    out.append(
        '<div style="font-family:Georgia,\'Times New Roman\',serif;'
        'max-width:600px;margin:0 auto;color:#1a1a1a;line-height:1.6;'
        'padding:0 20px">'
    )

    in_frontmatter = False
    i = 0
    while i < len(lines):
        line = lines[i]

        # ── YAML front-matter: skip entirely ──
        if i == 0 and line.strip() == "---":
            in_frontmatter = True
            i += 1
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
            i += 1
            continue

        stripped = line.strip()

        # ── Horizontal rule ──
        if stripped == "---":
            out.append('<hr style="border:none;border-top:1px solid #ddd;margin:24px 0">')
            i += 1
            continue

        # ── H1 (# but not ##) ──
        if stripped.startswith("# ") and not stripped.startswith("## "):
            text = _inline_md(stripped[2:])
            out.append(
                f'<h1 style="font-size:28px;font-weight:bold;margin:32px 0 8px;'
                f'color:#111;border-bottom:2px solid #222;padding-bottom:8px">{text}</h1>'
            )
            i += 1
            continue

        # ── H2 (## but not ###) ──
        if stripped.startswith("## ") and not stripped.startswith("### "):
            text = _inline_md(stripped[3:])
            out.append(
                f'<h2 style="font-size:20px;font-weight:bold;margin:28px 0 8px;'
                f'color:#222">{text}</h2>'
            )
            i += 1
            continue

        # ── H3 ──
        if stripped.startswith("### "):
            text = _inline_md(stripped[4:])
            out.append(
                f'<h3 style="font-size:17px;font-weight:bold;margin:20px 0 4px;'
                f'color:#333">{text}</h3>'
            )
            i += 1
            continue

        # ── Blockquote ──
        if stripped.startswith("> "):
            text = _inline_md(stripped[2:])
            out.append(
                f'<blockquote style="border-left:3px solid #555;padding:8px 16px;'
                f'margin:16px 0;background:#f7f7f7;color:#444;font-style:italic">'
                f'{text}</blockquote>'
            )
            i += 1
            continue

        # ── Sub-annotation line (starts with arrow symbol) ──
        if stripped.startswith("\u21b3"):
            text = _inline_md(stripped)
            out.append(
                f'<div style="padding-left:24px;margin:2px 0;'
                f'color:#555;font-style:italic">{text}</div>'
            )
            i += 1
            continue

        # ── Bullet list item ──
        if stripped.startswith("- ") or stripped.startswith("* "):
            content = stripped[2:]
            text = _inline_md(content)
            out.append(
                f'<div style="padding:3px 0;padding-left:16px">'
                f'<span style="color:#555;margin-right:6px">\u2022</span>{text}</div>'
            )
            i += 1
            continue

        # ── Full-line italic: *text* (star + non-space, not **) ──
        if (stripped.startswith("*") and not stripped.startswith("* ")
                and not stripped.startswith("**")
                and stripped.endswith("*")):
            text = stripped[1:-1]
            out.append(f'<p style="margin:8px 0;font-style:italic;color:#666">{text}</p>')
            i += 1
            continue

        # ── Empty line ──
        if not stripped:
            i += 1
            continue

        # ── Regular paragraph ──
        text = _inline_md(stripped)
        out.append(f'<p style="margin:8px 0">{text}</p>')
        i += 1

    out.append("</div>")
    return "\n".join(out)


def send_digest(md_content: str, subject: str | None = None):
    """
    Send the weekly digest via Resend.
    Body: .md rendered as HTML.
    Attachment: raw .md file for LLM handoff.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "")
    to_email = os.environ.get("RESEND_TO_EMAIL", "")

    if not all([api_key, from_email, to_email]):
        raise ValueError(
            "RESEND_API_KEY, RESEND_FROM_EMAIL, and RESEND_TO_EMAIL must all be set."
        )

    resend.api_key = api_key

    if subject is None:
        now = datetime.now(timezone.utc)
        subject = f"Weekly AI Download \u2014 {now.strftime('%b %d, %Y')}"

    html_body = _md_to_html(md_content)

    # Base64-encoded .md attachment
    md_b64 = base64.b64encode(md_content.encode("utf-8")).decode("ascii")
    filename = f"weekly_digest_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"

    params = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "attachments": [
            {
                "content": md_b64,
                "filename": filename,
            }
        ],
    }

    print(f"  Sending email to {to_email}...")
    response = resend.Emails.send(params)
    print(f"  Email sent. ID: {response.get('id', 'unknown')}")
    return response
