"""
email_templates.py
===================
Rendering for SONYA's branded emails (login-code, post-registration welcome).

Kept separate from email_sender.py (SMTP transport) so the markup can be
unit-tested and previewed without touching network code.

All dynamic values (code, expires_minutes, logo_url) are HTML-escaped before
being placed in the markup — none of it is ever raw-inserted.
"""
from __future__ import annotations

import os
from html import escape as _esc

SITE_URL = "https://sonya.group"
SUPPORT_EMAIL = "auth@sonya.group"

# SONYA brand banners, embedded as inline CID images — never a remote URL
# or data: URI. Two distinct assets/CIDs on purpose: the welcome banner
# (assets/email/welcome-banner.jpg) has "ДОБРО ПОЖАЛОВАТЬ" baked into the
# image, which only makes sense for a one-time post-registration email; the
# login banner (assets/email/login-banner.jpg) is the plain SONYA wordmark,
# used for the login-code email (which returning users see on every sign-in,
# not just registration). The actual image bytes are attached by
# email_sender.py; this module only knows the CIDs.
LOGIN_BANNER_CID = "login_banner"
WELCOME_BANNER_CID = "welcome_banner"
BRAND_BANNER_ALT = "SONYA"

# Pure grayscale/black palette — no brown/gold/warm tint anywhere.
# Card and code chip use a soft "glass" look: a translucent white overlay
# (rgba) layered on the black background for clients that support it,
# with a solid gray fallback declared first for Outlook's Word engine
# (which cannot parse rgba() and keeps the earlier valid declaration).
# Lighter grays + low-contrast borders + large radii keep the look soft
# and rounded rather than sharp/high-contrast.
_BG_OUTER = "#0D0D0E"
_TEXT = "#F2F2F2"
_TEXT_MUTED = "#9C9C9E"

_CARD_BG_FALLBACK = "#232326"
_CARD_BG_GLASS = "rgba(255,255,255,0.06)"
_CARD_BG = f"background-color:{_CARD_BG_FALLBACK};background-color:{_CARD_BG_GLASS};"
_CARD_RADIUS = "24px"

_BORDER_FALLBACK = "#333336"
_BORDER_GLASS = "rgba(255,255,255,0.10)"
_BORDER = f"border:1px solid {_BORDER_FALLBACK};border:1px solid {_BORDER_GLASS};"
_BORDER_TOP = f"border-top:1px solid {_BORDER_FALLBACK};border-top:1px solid {_BORDER_GLASS};"

_CODE_BG_FALLBACK = "#2A2A2D"
_CODE_BG_GLASS = "rgba(255,255,255,0.10)"
_CODE_BG = f"background-color:{_CODE_BG_FALLBACK};background-color:{_CODE_BG_GLASS};"
_CODE_RADIUS = "18px"

_CODE_BORDER_FALLBACK = "#3E3E42"
_CODE_BORDER_GLASS = "rgba(255,255,255,0.16)"
_CODE_BORDER = f"border:1px solid {_CODE_BORDER_FALLBACK};border:1px solid {_CODE_BORDER_GLASS};"

_FONT_STACK = (
    "'Sora',Arial,Helvetica,sans-serif"
)


def _logo_url(logo_url: str | None) -> str | None:
    if logo_url:
        return logo_url
    env_url = os.environ.get("EMAIL_LOGO_URL", "").strip()
    if env_url.startswith("https://"):
        return env_url
    return None


def _wordmark_html() -> str:
    return (
        "<span style=\"font-family:{font};font-size:22px;font-weight:700;"
        "letter-spacing:4px;color:{text};\">SONYA</span>"
    ).format(font=_FONT_STACK, text=_TEXT)


def render_login_code_email(
    code: str,
    expires_minutes: int,
    logo_url: str | None = None,
    subject: str | None = None,
    heading: str | None = None,
    banner_cid: str | None = None,
    banner_alt: str | None = None,
) -> tuple[str, str, str]:
    """
    Render the branded login-code email.

    Args:
        code: the numeric auth code (escaped before insertion, never raw HTML).
        expires_minutes: actual code TTL in minutes, sourced from backend
            config (scripts.auth_security.AUTH_CODE_TTL_SECONDS) by the caller.
        logo_url: absolute HTTPS URL to a logo image. Only used when
            banner_cid is not set. Falls back to EMAIL_LOGO_URL env var,
            then to a text wordmark if neither is set.
        subject: overrides the default subject line (e.g. for register/
            email_verify purposes that reuse this same branded layout).
        heading: overrides the default on-card heading text.
        banner_cid: when set, renders a full-width inline (CID) image as the
            header instead of the small logo/wordmark row — the caller
            (email_sender.send_auth_code_email) must attach the matching
            image bytes with this Content-ID for it to display. Takes
            priority over logo_url when both are set.
        banner_alt: alt text for the banner image. Defaults to
            BRAND_BANNER_ALT.

    Returns:
        (subject, html_body, text_body)
    """
    safe_code = _esc(str(code))
    safe_minutes = _esc(str(int(expires_minutes)))

    subject = subject or "Ваш код входа в SONYA"
    heading = heading or "Ваш код входа в SONYA"

    if banner_cid:
        safe_alt = _esc(banner_alt or BRAND_BANNER_ALT)
        header_row = f"""<tr>
<td style="padding:0;">
<img src="cid:{banner_cid}" width="600" alt="{safe_alt}" style="display:block;border:0;outline:none;text-decoration:none;width:100%;max-width:600px;height:auto;">
</td>
</tr>
<tr>"""
        card_top_padding = "32px 32px 40px 32px"
        outer_logo_block = ""
    else:
        resolved_logo = _logo_url(logo_url)
        if resolved_logo:
            safe_logo_url = _esc(resolved_logo, quote=True)
            logo_block = (
                "<img src=\"{url}\" width=\"140\" alt=\"SONYA\" "
                "style=\"display:block;border:0;outline:none;text-decoration:none;"
                "height:auto;max-width:140px;\">"
            ).format(url=safe_logo_url)
        else:
            logo_block = _wordmark_html()
        outer_logo_block = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">
<tr>
<td align="center" style="padding:0 0 28px 0;">
{logo_block}
</td>
</tr>
</table>

"""
        header_row = "<tr>"
        card_top_padding = "40px 32px"

    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:{_BG_OUTER};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_BG_OUTER};">
<tr>
<td align="center" style="padding:32px 16px;">

{outer_logo_block}<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{_CARD_BG_FALLBACK}" style="max-width:600px;width:100%;{_CARD_BG}{_BORDER}border-radius:{_CARD_RADIUS};overflow:hidden;">
{header_row}
<td style="padding:{card_top_padding};font-family:{_FONT_STACK};">

<h1 style="margin:0 0 24px 0;font-size:20px;line-height:28px;font-weight:600;color:{_TEXT};text-align:center;">
{_esc(heading)}
</h1>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td align="center" style="padding:0 0 24px 0;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" bgcolor="{_CODE_BG_FALLBACK}" style="{_CODE_BG}{_CODE_BORDER}border-radius:{_CODE_RADIUS};">
<tr>
<td style="padding:20px 40px;font-family:{_FONT_STACK};font-size:32px;line-height:38px;font-weight:700;letter-spacing:6px;color:{_TEXT};text-align:center;">
{safe_code}
</td>
</tr>
</table>
</td>
</tr>
</table>

<p style="margin:0 0 16px 0;font-family:{_FONT_STACK};font-size:14px;line-height:22px;color:{_TEXT_MUTED};text-align:center;">
Код действителен {safe_minutes} мин. и может быть использован один раз.
</p>

<p style="margin:0 0 28px 0;font-family:{_FONT_STACK};font-size:14px;line-height:22px;color:{_TEXT_MUTED};text-align:center;">
Если вы не запрашивали этот код, просто проигнорируйте письмо.
</p>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="{_BORDER_TOP}">
<tr>
<td style="padding:24px 0 0 0;font-family:{_FONT_STACK};font-size:13px;line-height:20px;color:{_TEXT_MUTED};text-align:center;">
<a href="{SITE_URL}" style="color:{_TEXT_MUTED};text-decoration:underline;">sonya.group</a>
&nbsp;·&nbsp;
<a href="mailto:{SUPPORT_EMAIL}" style="color:{_TEXT_MUTED};text-decoration:underline;">{SUPPORT_EMAIL}</a>
</td>
</tr>
</table>

</td>
</tr>
</table>

</td>
</tr>
</table>
</body>
</html>"""

    text = (
        "SONYA\n\n"
        f"{heading}\n\n"
        f"    {code}\n\n"
        f"Код действителен {int(expires_minutes)} мин. и может быть использован один раз.\n\n"
        "Если вы не запрашивали этот код, просто проигнорируйте письмо.\n\n"
        f"{SITE_URL}\n"
        f"Поддержка: {SUPPORT_EMAIL}\n"
    )

    return subject, html, text


# ── Welcome email (sent once, after registration) ──────────————————————————
#
# Distinct look from the login-code email above: this one is requested as a
# premium, minimalist, strictly black-and-white layout — not the dark
# "glass" card used for the login code.

_W_BG_OUTER_LIGHT = "#FFFFFF"
_W_BG_CARD_LIGHT = "#FFFFFF"
_W_BORDER_LIGHT = "#EAEAEA"
_W_TEXT_LIGHT = "#111111"
_W_TEXT_MUTED_LIGHT = "#6B6B6E"

_W_BG_OUTER_DARK = "#0D0D0E"
_W_BG_CARD_DARK = "#1A1A1C"
_W_BORDER_DARK = "#2E2E30"
_W_TEXT_DARK = "#F2F2F2"
_W_TEXT_MUTED_DARK = "#9C9C9E"

_W_RADIUS = "24px"


def render_welcome_email() -> tuple[str, str, str]:
    """
    Render the branded post-registration welcome email.

    The banner image is referenced as `cid:{WELCOME_BANNER_CID}` — the
    caller (email_sender.send_welcome_email) must attach the actual image
    bytes with a matching Content-ID for it to display inline.

    Returns:
        (subject, html_body, text_body)
    """
    subject = "Добро пожаловать в SONYA"
    heading = "Добро пожаловать в SONYA"
    subheading = "Ваш AI-инструмент для автоматической обработки и монтажа видео"

    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_esc(subject)}</title>
<style>
  @media (prefers-color-scheme: dark) {{
    body, .sonya-outer {{ background-color:{_W_BG_OUTER_DARK} !important; }}
    .sonya-card {{ background-color:{_W_BG_CARD_DARK} !important; border-color:{_W_BORDER_DARK} !important; }}
    .sonya-heading {{ color:{_W_TEXT_DARK} !important; }}
    .sonya-muted, .sonya-link {{ color:{_W_TEXT_MUTED_DARK} !important; }}
    .sonya-divider {{ border-color:{_W_BORDER_DARK} !important; }}
  }}
  @media only screen and (max-width:600px) {{
    .sonya-pad {{ padding-left:24px !important; padding-right:24px !important; }}
    .sonya-outer-pad {{ padding-left:12px !important; padding-right:12px !important; }}
  }}
</style>
</head>
<body class="sonya-outer" style="margin:0;padding:0;background-color:{_W_BG_OUTER_LIGHT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="sonya-outer" style="background-color:{_W_BG_OUTER_LIGHT};">
<tr>
<td align="center" class="sonya-outer-pad" style="padding:32px 16px;">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{_W_BG_CARD_LIGHT}" class="sonya-card" style="max-width:600px;width:100%;background-color:{_W_BG_CARD_LIGHT};border:1px solid {_W_BORDER_LIGHT};border-radius:{_W_RADIUS};overflow:hidden;">
<tr>
<td style="padding:0;">
<img src="cid:{WELCOME_BANNER_CID}" width="600" alt="{_esc(BRAND_BANNER_ALT)}" style="display:block;border:0;outline:none;text-decoration:none;width:100%;max-width:600px;height:auto;">
</td>
</tr>
<tr>
<td class="sonya-pad" style="padding:48px 40px 40px 40px;font-family:{_FONT_STACK};">

<h1 class="sonya-heading" style="margin:0 0 16px 0;font-size:26px;line-height:34px;font-weight:700;color:{_W_TEXT_LIGHT};text-align:center;">
{_esc(heading)}
</h1>

<p class="sonya-muted" style="margin:0 0 40px 0;font-size:16px;line-height:26px;color:{_W_TEXT_MUTED_LIGHT};text-align:center;">
{_esc(subheading)}
</p>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="sonya-divider" style="border-top:1px solid {_W_BORDER_LIGHT};">
<tr>
<td style="padding:24px 0 0 0;font-family:{_FONT_STACK};font-size:13px;line-height:20px;text-align:center;">
<a href="{SITE_URL}" class="sonya-link" style="color:{_W_TEXT_MUTED_LIGHT};text-decoration:underline;">sonya.group</a>
&nbsp;·&nbsp;
<a href="mailto:{SUPPORT_EMAIL}" class="sonya-link" style="color:{_W_TEXT_MUTED_LIGHT};text-decoration:underline;">{SUPPORT_EMAIL}</a>
</td>
</tr>
</table>

</td>
</tr>
</table>

</td>
</tr>
</table>
</body>
</html>"""

    text = (
        "SONYA\n\n"
        f"{heading}\n\n"
        f"{subheading}\n\n"
        f"{SITE_URL}\n"
        f"Поддержка: {SUPPORT_EMAIL}\n"
    )

    return subject, html, text
