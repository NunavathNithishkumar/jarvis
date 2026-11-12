"""Broker-side executor for Phase 3 system actions (browser / media).

Only the broker calls this module, after policy classification. It never takes a
shell command: URLs are validated by policy and handed to the Windows default
browser via ShellExecute (os.startfile) with an http(s) URL only.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass

YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query="
_VIDEO_RE = re.compile(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"')
_TITLE_RE = re.compile(r'"videoRenderer":\{"videoId":"[A-Za-z0-9_-]{11}".*?"title":\{"runs":\[\{"text":"(.*?)"\}', re.S)


class SystemActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass
class VideoHit:
    video_id: str
    title: str | None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class SystemActions:
    """Real implementation. Tests inject a fake with the same two methods."""

    def open_url(self, url: str) -> None:
        if not re.match(r"^https?://", url, re.I):
            raise SystemActionError("BAD_URL", "only http(s) URLs can be opened")
        if os.name != "nt":
            import webbrowser
            webbrowser.open(url)
            return
        os.startfile(url)  # noqa: S606 - default browser via ShellExecute; URL validated by policy

    def youtube_search(self, query: str, timeout_s: float = 15.0) -> VideoHit:
        import httpx
        url = YOUTUBE_SEARCH + urllib.parse.quote_plus(query)
        try:
            r = httpx.get(url, timeout=timeout_s, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"})
        except httpx.HTTPError as e:
            raise SystemActionError("NETWORK_ERROR", f"YouTube search failed: {type(e).__name__}")
        m = _VIDEO_RE.search(r.text)
        if not m:
            raise SystemActionError("NO_RESULTS", "no video found for that search")
        t = _TITLE_RE.search(r.text[m.start(): m.start() + 20000])
        title = None
        if t:
            import json
            try:
                title = json.loads('"' + t.group(1) + '"')
            except ValueError:
                title = t.group(1)
        return VideoHit(m.group(1), title)
