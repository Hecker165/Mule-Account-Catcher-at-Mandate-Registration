"""A10 static secret scans over apps/api/app and apps/web/src.

Every pattern is tight and documented: the goal is to fail loudly on
secret-printing or payload-logging code shapes while never flagging the
disciplined request-ID/outcome log statements the codebase uses. Line-based
scans strip whole-line comments first; the log-statement scan is
statement-aware (balanced-parentheses extraction) so multi-line log calls
are fully inspected.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_API_APP_DIR = _ROOT / "apps" / "api" / "app"
_WEB_SRC_DIR = _ROOT / "apps" / "web" / "src"
_ENV_EXAMPLE = _ROOT / ".env.example"
_CI_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"

_POSTGRES_DEV_URL = (
    "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
)
_REDIS_DEV_URL = "redis://localhost:6379/0"
_PLACEHOLDER_PREFIXES = ("replace-with", "changeme", "change-me", "your-", "<", "dummy")

_LOG_CALL_RE = re.compile(
    r"\b(?:logger|logging|log)\.(?:debug|info|warning|warn|error|exception|critical)\s*\("
)


def _python_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in _API_APP_DIR.rglob("*.py")}


def _web_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(_WEB_SRC_DIR.rglob("*"))
        if path.suffix in {".ts", ".tsx"} and path.is_file()
    }


def _strip_comment_lines(text: str) -> str:
    """Drop whole-line ``#`` comments so documented patterns are not self-tripped."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _log_statements(text: str) -> list[str]:
    """Extract each log call statement in full (multi-line aware).

    Rationale: multi-line ``logger.info(...)`` calls must be scanned whole;
    scanning only the opening line would miss payload arguments.
    """
    statements: list[str] = []
    for match in _LOG_CALL_RE.finditer(text):
        depth = 0
        index = match.end() - 1
        while index < len(text):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        statements.append(text[match.start() : index + 1])
    return statements


def test_no_print_statements_in_api_app() -> None:
    offenders = [
        path.name
        for path, text in _python_sources().items()
        if re.search(r"(?m)^\s*print\s*\(", _strip_comment_lines(text))
    ]
    assert offenders == [], f"print() calls in app code: {offenders}"


def test_no_console_log_in_web_src() -> None:
    offenders = [
        path.name
        for path, text in _web_sources().items()
        if "console.log(" in _strip_comment_lines(text)
    ]
    assert offenders == [], f"console.log() in web src: {offenders}"


def test_no_secret_value_logging_patterns() -> None:
    """Log statements must never carry secret values, signatures or payloads.

    ``get_secret_value`` results and webhook signatures are credential
    material; ``payload``/``vpa`` identify raw customer data. The app's
    disciplined statements log only IDs, statuses and fixed strings.
    """
    forbidden = ("get_secret_value", "signature", "payload", "vpa")
    offenders: list[str] = []
    for path, text in _python_sources().items():
        for statement in _log_statements(_strip_comment_lines(text)):
            hits = [word for word in forbidden if word in statement]
            if hits:
                offenders.append(f"{path.name}: {hits}")
    assert offenders == [], f"secret/payload material in log statements: {offenders}"


def test_no_live_razorpay_keys_anywhere_in_apps() -> None:
    # Matches only real key material: a live Razorpay key ID carries >= 14
    # characters after the prefix. Bare "rzp_live_" literals in refusal-guard
    # code, tests or docs are references to the pattern, not credentials.
    live_key_re = re.compile(r"rzp_live_[A-Za-z0-9]{14,}")
    offenders: list[Path] = []
    for scan_dir in (_ROOT / "apps", _ROOT / "data", _ROOT / "docs"):
        for path in _walk_text_files(scan_dir):
            # errors="replace": git-ignored local caches (ruff/mypy) are binary
            # and irrelevant to key material; a decode failure must not mask a scan.
            if live_key_re.search(path.read_bytes().decode("utf-8", errors="replace")):
                offenders.append(path)
    assert offenders == [], f"live Razorpay key material found: {offenders}"


_SKIP_DIRS = frozenset(
    {"node_modules", "__pycache__", ".venv", ".next", ".git", "generated", ".pytest_cache"}
)


def _walk_text_files(scan_dir: Path) -> list[Path]:
    """Walk a source tree, skipping dependency and cache directories.

    Rationale: ``node_modules``/``.venv`` are third-party material (covered by
    the dependency audit job, not the secret scan) and can be huge/binary.
    """
    return [
        path
        for path in scan_dir.rglob("*")
        if path.is_file() and not any(part in _SKIP_DIRS for part in path.parts)
    ]


def test_no_test_secret_in_production_source() -> None:
    offenders = [
        path.name
        for path, text in {**_python_sources(), **_web_sources()}.items()
        if "local-test-secret" in text
    ]
    assert offenders == [], f"test secret in production source: {offenders}"


def test_env_example_contains_placeholder_values_only() -> None:
    values: dict[str, str] = {}
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    for key in (
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
        "LITELLM_API_KEY",
        "HMAC_PEPPER",
    ):
        value = values.get(key, "")
        lowered = value.lower()
        assert value == "" or lowered.startswith(_PLACEHOLDER_PREFIXES), (
            f"{key} must be empty or a placeholder, got {value!r}"
        )
    assert values.get("POSTGRES_URL") == _POSTGRES_DEV_URL
    assert values.get("REDIS_URL") == _REDIS_DEV_URL


def test_no_hardcoded_credentials_in_ci() -> None:
    """CI may contain only the documented dummy values (local-test-secret)."""
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "rzp_live_" not in text
    assert "rzp_test_" not in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("RAZORPAY_WEBHOOK_SECRET:"):
            assert stripped == "RAZORPAY_WEBHOOK_SECRET: local-test-secret"
        if any(stripped.startswith(f"POSTGRES_{part}:") for part in ("USER", "PASSWORD", "DB")):
            assert stripped.endswith("mandate_guardian")
        if stripped.startswith(("RAZORPAY_KEY_ID:", "RAZORPAY_KEY_SECRET:")):
            assert stripped in ("RAZORPAY_KEY_ID:", "RAZORPAY_KEY_SECRET:")
