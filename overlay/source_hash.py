"""Print a short hash of what goes into the overlay image besides PostHog: the Dockerfile and the module."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def source_hash(root: Path) -> str:
    files = [root / "Dockerfile", *sorted((root / "posthog_k8s_authproxy").rglob("*.py"))]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:12]


if __name__ == "__main__":
    sys.stdout.write(source_hash(Path(__file__).resolve().parent) + "\n")
