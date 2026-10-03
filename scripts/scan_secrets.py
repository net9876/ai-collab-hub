"""Pre-push scan of tracked files for secrets and private identifiers.

Fails (exit 1) on: credentials/tokens/keys, JWTs, private keys, non-placeholder
GUIDs (subscription/tenant/object IDs), and e-mail addresses. Known public
identifiers are allowlisted below. A line can opt out with `gitleaks:allow`
only for documented public test values.

Usage: python scripts/scan_secrets.py            # git ls-files
       python scripts/scan_secrets.py --all      # also untracked, not ignored
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = {
    "storage account key": re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}"),
    "SAS signature": re.compile(r"[?&]sig=[A-Za-z0-9%+/=]{20,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "client secret": re.compile(r"(?i)client_secret\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    "password assignment": re.compile(r"(?i)\bpassword\s*[:=]\s*['\"][^'\"]{6,}['\"]"),
    "OpenAI/Anthropic key": re.compile(r"\b(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})"),
}
GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

PUBLIC_GUIDS = {
    "04b07795-8ddb-461a-bbee-02f9e1bf7b46",  # Azure CLI public client ID
    "37f7f235-527c-4136-accd-4a02d197296e",  # Microsoft Graph delegated 'openid' scope ID
}
EMAIL_OK = re.compile(
    r"(noreply@anthropic\.com|@example\.(com|test|org)|\.example$|@users\.noreply\.github\.com)$"
)
SKIP_SUFFIXES = {".lock.hcl", ".png", ".jpg", ".ico"}


def placeholder_guid(g: str) -> bool:
    body = g.replace("-", "").lower()
    return body.startswith("0000000") or len(set(body)) <= 3


def files(include_untracked: bool) -> list[Path]:
    args = ["git", "ls-files", "-z"]
    if include_untracked:
        args = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, check=True).stdout  # noqa: S603
    return [ROOT / p for p in out.decode().split("\0") if p]


def main() -> int:
    findings: list[str] = []
    for path in files("--all" in sys.argv):
        if any(str(path).endswith(s) for s in SKIP_SUFFIXES) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)
        for n, line in enumerate(text.splitlines(), 1):
            if "gitleaks:allow" in line:
                continue
            for name, pat in SECRET_PATTERNS.items():
                if pat.search(line):
                    findings.append(f"{rel}:{n}: {name}")
            for g in GUID.findall(line):
                if g.lower() not in PUBLIC_GUIDS and not placeholder_guid(g):
                    findings.append(f"{rel}:{n}: non-placeholder GUID {g[:8]}...")
            for e in EMAIL.findall(line):
                if not EMAIL_OK.search(e):
                    findings.append(f"{rel}:{n}: e-mail address")
    # The previous line of a gitleaks:allow comment may hold the value (wrapped strings).
    findings = _drop_allowed_continuations(findings)
    for f in findings:
        print(f)
    print(f"scan_secrets: {len(findings)} finding(s)")
    return 1 if findings else 0


def _drop_allowed_continuations(findings: list[str]) -> list[str]:
    kept = []
    for f in findings:
        rel, line_no, _ = f.split(":", 2)
        path = ROOT / rel
        lines = path.read_text(encoding="utf-8").splitlines()
        idx = int(line_no) - 1
        if idx > 0 and "gitleaks:allow" in lines[idx - 1]:
            continue
        kept.append(f)
    return kept


if __name__ == "__main__":
    sys.exit(main())
