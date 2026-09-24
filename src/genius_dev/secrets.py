"""Secret detection, redaction and credential lookup (env vars / macOS Keychain)."""
from __future__ import annotations

import fnmatch
import logging
import os
import re

SERVICE = "genius-dev"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{24,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    (   # quoted assignments in any language:  api_key = "…", "password": "…"
        "assignment",
        re.compile(
            r"""(?ix)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|passwd|password|private[_-]?key)[A-Za-z0-9_]*)["']?
            \s*[:=]\s*(['"])([^\s'"]{10,})\2"""
        ),
    ),
    (   # dotenv style, unquoted:  SOME_API_KEY=abc123…   (upper-case names only, so ordinary code assignments are not flagged)
        "assignment",
        re.compile(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_?KEY)[A-Z0-9_]*)\s*=\s*()([^\s'\"#(),;]{12,})\s*$"),
    ),
]

SECRET_FILE_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "credentials*", "*.keystore", ".npmrc", ".pypirc", "secrets.*", "*.secret", ".netrc",
]

# A file whose extension marks it as source code is never a "secret file" by name, even if it happens to be called
# secrets.py / credentials.js / etc. (an ordinary module implementing secret-handling logic, not a literal credential
# dump). Bug found by running Genius Dev's own `finish` audit on itself: it flagged src/genius_dev/secrets.py.
_CODE_EXTS = {"py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "rb", "java", "kt", "kts", "swift",
              "c", "h", "hpp", "cc", "cpp", "cs", "php", "sh", "bash", "zsh", "pl", "lua", "dart", "scala", "m", "mm"}

DEFAULT_EXCLUDED_DIRS = [
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
    "target", ".genius", ".pytest_cache", ".mypy_cache", ".gradle", "Pods", ".idea",
]


_FAKE_MARKERS = ("abcdefghijklmnop", "0123456789", "hunter2", "secret", "fake", "dummy", "example", "test", "xxxx", "1234567890", "redacted", "placeholder", "changeme", "your-", "your_")  # genius:ignore


def looks_fake(token: str) -> bool:
    """Obviously fabricated credentials (docs, fixtures, tests): alphabet runs, 'secret'/'fake' words, or almost no character variety."""
    low = token.lower()
    if low.startswith(("env:", "keychain:", "none")):            # references to where a key lives, not a key
        return True
    if token.startswith("-----BEGIN") and len(token) < 120:      # a "private key" too short to be real
        return True
    return any(m in low for m in _FAKE_MARKERS) or len(set(low)) < 7


def find_secrets(text: str) -> list[str]:
    return sorted({kind for kind, rx in _PATTERNS if rx.search(text)})


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a marker."""
    if not text:
        return text
    for kind, rx in _PATTERNS:
        if kind == "assignment":
            text = rx.sub(lambda m: m.group(0) if m.group(3).startswith(("env:", "keychain:")) else f"{m.group(1)}={m.group(2)}[REDACTED]{m.group(2)}", text)
        else:
            text = rx.sub(f"[REDACTED:{kind}]", text)
    return text


def is_secret_file(path: str) -> bool:
    name = os.path.basename(path)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _CODE_EXTS:
        return False
    return any(fnmatch.fnmatch(name, g) for g in SECRET_FILE_GLOBS)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(a)) if isinstance(a, str) else a for a in record.args)
        return True


# --- credential storage -------------------------------------------------------

def keychain_set(account: str, secret: str) -> bool:
    try:
        import keyring

        keyring.set_password(SERVICE, account, secret)
        return True
    except Exception:
        return False


def keychain_get(account: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(SERVICE, account)
    except Exception:
        return None


def keychain_delete(account: str) -> bool:
    try:
        import keyring

        keyring.delete_password(SERVICE, account)
        return True
    except Exception:
        return False


def has_key(ref: str | None) -> bool:
    """True when the credential behind a key reference exists. Never returns the value."""
    return bool(resolve_key(ref))


def resolve_key(ref: str | None) -> str | None:
    """Resolve an API-key reference. Accepts ``env:NAME``, ``keychain:account`` or ``none``.

    Literal keys are deliberately *not* accepted in config files.
    """
    if not ref or ref == "none":
        return None
    kind, _, val = ref.partition(":")
    if kind == "env":
        return os.environ.get(val) or None
    if kind == "keychain":
        return keychain_get(val)
    return None
