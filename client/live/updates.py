"""Ask the project's GitHub releases page whether a newer VoxChat is out.

Release builds carry their tag in assets/version.txt (written by the build workflow).
Runs from source have no such file and skip the check: they update with git.
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.request

LATEST = 'https://api.github.com/repos/ardazafer123/voxchat/releases/latest'
VERSION_FILE = Path(__file__).resolve().parent.parent / 'assets' / 'version.txt'


def current_version() -> str | None:
    try:
        return VERSION_FILE.read_text(encoding='utf-8').strip() or None
    except OSError:
        return None


def parse(tag: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in tag.strip().lstrip('v').split('.'))
    except (AttributeError, ValueError):
        return None


def newer(current: str, latest: str) -> bool:
    mine, theirs = parse(current), parse(latest)
    return bool(mine and theirs and theirs > mine)


def latest_release() -> tuple[str, str]:
    """Return (tag, page URL) of the newest published release."""
    request = urllib.request.Request(LATEST, headers={'Accept': 'application/vnd.github+json',
                                                      'User-Agent': 'VoxChat'})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    return data['tag_name'], data['html_url']
