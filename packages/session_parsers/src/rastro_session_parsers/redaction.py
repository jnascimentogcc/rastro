"""Best-effort secret scrubbing applied to every text before it leaves the machine.

Two layers. Our own patterns catch well-known token formats, credentials in URLs and
`NAME=value` assignments with sensitive names. Then the `detect-secrets` plugins (maintained
detectors for cloud, VCS, payment and messaging providers) catch what the patterns do not know
about. Entropy-based plugins are left out on purpose: in prose they flag identifiers and hashes.
Neither layer can catch every secret, which is why tool results, file contents and edit
payloads are never sent in the first place.
"""

import logging
import re

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"

_WHOLE_MATCH_PATTERNS = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
]

# scheme://user:password@host  ->  keep everything except the password
_URL_CREDENTIALS = re.compile(
    r"(?P<head>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(?P<tail>@)"
)

# password=..., "api_key": "...", OPENAI_API_KEY=...  ->  keep the name, drop the value
_SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<name>[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key
                       |credential|authorization)[\w.-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"'`,;&}]{6,})
    """
)


_DETECTORS = [
    "AWSKeyDetector",
    "ArtifactoryDetector",
    "AzureStorageKeyDetector",
    "BasicAuthDetector",
    "CloudantDetector",
    "DiscordBotTokenDetector",
    "GitHubTokenDetector",
    "GitLabTokenDetector",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "JwtTokenDetector",
    "MailchimpDetector",
    "NpmDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "PypiTokenDetector",
    "SendGridDetector",
    "SlackDetector",
    "SoftlayerDetector",
    "SquareOAuthDetector",
    "StripeDetector",
    "TelegramBotTokenDetector",
    "TwilioKeyDetector",
]
_SETTINGS = {"plugins_used": [{"name": name} for name in _DETECTORS]}
_MIN_SECRET_LENGTH = 8  # some plugins report only a prefix of the match; never redact that


def _detected_secrets(text: str) -> set[str]:
    found: set[str] = set()
    try:
        with transient_settings(_SETTINGS):
            for line in text.splitlines():
                for secret in scan_line(line):
                    value = secret.secret_value
                    if value and len(value) >= _MIN_SECRET_LENGTH:
                        found.add(value)
    except Exception:  # the library must never break a capture
        logger.warning("secret detection failed; relying on pattern redaction", exc_info=True)
    return found


def redact(text: str) -> str:
    for pattern in _WHOLE_MATCH_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m['head']}{REDACTED}{m['tail']}", text)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda m: f"{m['name']}{m['sep']}{REDACTED}" if m["value"] != REDACTED else m[0], text
    )
    for secret in sorted(_detected_secrets(text), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text
