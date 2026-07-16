"""Render .md as HTML email body + attach raw .md, send via Resend."""

from __future__ import annotations

import base64
import os
import re
import time
from datetime import datetime, timezone

import requests

from common.logging_setup import get_logger, log_event
from common.resilience import RetryError, retry

try:  # resend.exceptions is used only by the retry predicate.
    import resend.exceptions as resend_exceptions
except ImportError:  # pragma: no cover - resend is installed in this project.
    resend_exceptions = None

# Retry sleep hook only. Tests monkeypatch ``deliver.send_email._SLEEP`` to a
# no-op so retry backoff runs instantly.
_SLEEP = time.sleep

logger = get_logger(__name__)

RESEND_TIMEOUT_S = 30
RESEND_API_URL = "https://api.resend.com/emails"


class DeliveryError(RuntimeError):
    """Raised when the digest could not be delivered (transient or permanent)."""


def _is_retryable_resend(exc: BaseException) -> bool:
    """True only for transient Resend/HTTP errors worth retrying.

    Timeouts and connection errors are always transient. HTTP errors are
    transient only for 429 (rate limit) and 5xx (server) responses. Resend SDK
    rate-limit / 5xx errors are transient too; auth, validation, and other 4xx
    are permanent and re-raise immediately.
    """
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        if response is None:
            return False
        status = response.status_code
        return status == 429 or status >= 500
    if resend_exceptions is not None:
        if isinstance(exc, resend_exceptions.RateLimitError):
            return True
        if isinstance(exc, resend_exceptions.ResendError):
            try:
                code = int(getattr(exc, "code", 0) or 0)
            except (TypeError, ValueError):
                return False
            return code == 429 or code >= 500
    return False


def _notify_delivery_failure(err):
    """Extension seam for proactive alerting on delivery failure (no-op today)."""
    return None


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


def send_digest(md_content: str, subject: str | None = None) -> dict:
    """
    Send the weekly digest via Resend.
    Body: .md rendered as HTML.
    Attachment: raw .md file for LLM handoff.

    Transient failures are retried with backoff; on ultimate failure (or a
    permanent error) a :class:`DeliveryError` is raised for the caller to
    record. Returns the parsed Resend response dict on success.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "")
    to_email = os.environ.get("RESEND_TO_EMAIL", "")

    if not all([api_key, from_email, to_email]):
        raise ValueError(
            "RESEND_API_KEY, RESEND_FROM_EMAIL, and RESEND_TO_EMAIL must all be set."
        )

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

    # Resend SDK exposes no request timeout, so call the HTTP API directly and
    # wrap it in retry-with-backoff for transient errors.
    @retry(
        retry_on=_is_retryable_resend,
        logger=logger,
        sleep=lambda s: _SLEEP(s),
    )
    def _send():
        r = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=params,
            timeout=RESEND_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()

    try:
        response = _send()
    except RetryError as e:
        raise DeliveryError(f"delivery failed after retries: {e.last_exc}") from e

    if not isinstance(response, dict) or not response.get("id") or (
        response.get("error") or response.get("message")
    ):
        raise DeliveryError(f"Resend returned no id: {response}")

    log_event(logger, "deliver", "send", outcome="ok", email_id=response.get("id"))
    return response
