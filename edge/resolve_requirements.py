"""Prefer supplied wheel files while keeping the deployed lock's SHA-256 hashes."""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

source = Path("/tmp/policy.requirements")
wheelhouse = Path("/tmp/policy_wheels")
origin = os.environ["POLICY_WHEEL_ORIGIN"].rstrip("/")


def locate(match):
    url = match[0]
    path = Path(urlparse(url).path)
    local = wheelhouse / path.name
    return local.as_uri() if local.is_file() else origin + path.as_posix()


Path("/tmp/policy.resolved").write_text(
    re.sub(r"https://files\.pythonhosted\.org/[^\s]+\.whl", locate, source.read_text())
)
