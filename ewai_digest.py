#!/usr/bin/env python3
"""ewai-digest — weekly AI topic digest for European Women in AI (EWAI).

Pipeline (each stage is its own function so sources and weighting can be
swapped or tuned independently):

    1. fetch_insider_feeds()      -> pull the latest from 4 AI-industry sources
    2. fetch_mainstream_signal()  -> targeted web searches on EWAI focus areas
    3. cross_reference(...)        -> Claude picks the most actionable topic
    4. deep_dive(topic)            -> Claude + web search gather the details
    5. generate_whatsapp_message() -> reframe through EWAI's mission & voice

Run it manually from the terminal:

    export ANTHROPIC_API_KEY=sk-ant-...
    python3 ewai_digest.py

Every stage prints what it found so you can watch the pipeline work before it
gets put on a schedule.
"""

from __future__ import annotations

import html
import os
import re
import sys
import textwrap
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - dependency hint
    BeautifulSoup = None  # type: ignore

try:
    import anthropic
except ImportError:  # pragma: no cover - dependency hint
    anthropic = None  # type: ignore


# --------------------------------------------------------------------------- #
# Configuration — edit these to swap sources, tune weighting, or change voice. #
# --------------------------------------------------------------------------- #

# Default model. Sonnet 5 is a good quality/cost balance — roughly half the
# price of Opus. Swap it with the EWAI_MODEL env var:
#   higher quality:  EWAI_MODEL=claude-opus-4-8
#   cheapest:        EWAI_MODEL=claude-haiku-4-5
MODEL = os.environ.get("EWAI_MODEL", "claude-sonnet-5")

# Capability groups so MODEL can be swapped freely — they pick the right
# web-search tool version and thinking config for whichever model is set.
# The web_search_20260209 variant (dynamic filtering) and adaptive thinking
# need newer models; older/cheaper models fall back gracefully.
_DYNAMIC_WEB_SEARCH_MODELS = ("opus-4-8", "opus-4-7", "opus-4-6", "sonnet-5", "sonnet-4-6")
_ADAPTIVE_THINKING_MODELS = ("fable", "opus-4-8", "opus-4-7", "opus-4-6", "sonnet-5", "sonnet-4-6")

USER_AGENT = "ewai-digest/1.0 (+https://europeanwomeninai.org)"

# RSS/Atom feeds — (source label, feed URL).
RSS_FEEDS = [
    ("The Rundown AI", "https://rss.beehiiv.com/feeds/2R3C6Bt5wj.xml"),
    (
        "MIT Technology Review (AI)",
        "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    ),
]

# Pages we scrape (no public feed / login-free HTML).
SCRAPE_SOURCES = [
    ("The Batch (DeepLearning.AI)", "https://www.deeplearning.ai/the-batch"),
    ("AlphaSignal", "https://alphasignal.ai/last-email"),
]

# The searches run in stage 2 — matched to EWAI's real focus areas, not
# generic "AI news". Tune these to reshape what the mainstream signal covers.
MAINSTREAM_QUERIES = [
    ("AI + investing/finance", "how people are using AI in personal investing and finance this week"),
    ("AI + jobs/careers", "AI impact on jobs, careers and hiring this week"),
    ("AI tools for professionals", "new practical AI tools for non-technical professionals this week"),
    ("AI regulation/policy", "AI regulation and policy developments this week, especially Europe"),
]

MAX_ITEMS_PER_SOURCE = 8

# EWAI mission + brand voice, injected into the Claude prompts so every stage
# reasons from the community's point of view.
EWAI_MISSION = textwrap.dedent(
    """\
    European Women in AI (EWAI) is a Switzerland-based peer community helping
    women navigate AI at key career and life inflection points. The goal is
    confidence and capability to lead in investing, learning, and working with
    AI — with a focus on real-world adoption over technical depth. Members are
    smart and ambitious but often new to AI; they care about what they can
    actually do differently in their finances, careers, and daily lives.
    """
)

EWAI_VOICE = "Purposeful, Warm, Courageous"

# Weighting rules for stage 3. Edit this string to re-tune topic selection.
SELECTION_WEIGHTING = textwrap.dedent(
    """\
    Weight for SIGNIFICANCE and AWARENESS VALUE for the community — the topic
    does NOT need to map to finance, career, or daily-life advice to win.

    - Topics appearing in BOTH insider and mainstream sources are the strongest
      candidates (real + relevant to a non-technical audience).

    - Mainstream-only topics can still matter to our audience even if AI insiders
      consider them old news — do not dismiss them.

    - Insider-only topics should be DEPRIORITIZED unless they are major safety or
      regulatory news that a non-technical woman genuinely needs to know about.

    Prefer whichever topic is most significant for AI awareness broadly — something
    that shifts the landscape, changes what's possible, or represents a meaningful
    shift in how AI is being built, regulated, or adopted. The goal is for members
    to be informed and able to engage in conversation about what matters right now.
    A direct action step is a bonus, not a requirement.
    """
)


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class FeedItem:
    source: str
    title: str
    url: str = ""
    summary: str = ""

    def as_line(self) -> str:
        bits = f"[{self.source}] {self.title}".strip()
        if self.url:
            bits += f" ({self.url})"
        return bits


@dataclass
class DigestResult:
    insider: list[FeedItem] = field(default_factory=list)
    mainstream: list[dict[str, str]] = field(default_factory=list)
    cross_reference: dict[str, Any] = field(default_factory=dict)
    deep_dive: str = ""
    whatsapp_message: str = ""


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #

def _print_header(step: str) -> None:
    print("\n" + "=" * 72)
    print(step)
    print("=" * 72)


def _clean_text(raw: str, limit: int = 400) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _client() -> "anthropic.Anthropic":
    if anthropic is None:
        sys.exit("The 'anthropic' package is required. Install it: pip install anthropic")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY (or run "
            "`ant auth login`) before running stages 2-5."
        )
    return anthropic.Anthropic()


def _text_from_response(response: Any) -> str:
    """Concatenate all text blocks from a Messages API response."""
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in parts if p).strip()


def _web_search_tool(max_uses: int = 5) -> dict[str, Any]:
    """Pick the web_search tool version the current MODEL supports."""
    version = (
        "web_search_20260209"
        if any(m in MODEL for m in _DYNAMIC_WEB_SEARCH_MODELS)
        else "web_search_20250305"
    )
    return {"type": version, "name": "web_search", "max_uses": max_uses}


def _thinking_kwargs() -> dict[str, Any]:
    """Adaptive thinking for models that support it; nothing otherwise."""
    if any(m in MODEL for m in _ADAPTIVE_THINKING_MODELS):
        return {"thinking": {"type": "adaptive"}}
    return {}


def _run_with_web_search(client: Any, prompt: str, max_uses: int = 5) -> str:
    """One Claude turn with the server-side web_search tool.

    Handles the `pause_turn` stop reason (server-tool sampling loop hit its
    iteration cap) by resending until Claude finishes.
    """
    tool = _web_search_tool(max_uses)
    messages = [{"role": "user", "content": prompt}]
    for _ in range(6):  # bound the continuation loop
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            tools=[tool],
            messages=messages,
            **_thinking_kwargs(),
        )
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        return _text_from_response(response)
    return _text_from_response(response)


# --------------------------------------------------------------------------- #
# Stage 1 — insider feeds                                                     #
# --------------------------------------------------------------------------- #

def fetch_rss(source: str, url: str, limit: int = MAX_ITEMS_PER_SOURCE) -> list[FeedItem]:
    """Parse an RSS 2.0 or Atom feed with the standard library."""
    items: list[FeedItem] = []
    try:
        raw = _http_get(url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"  ! could not fetch {source}: {exc}")
        return items

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"  ! could not parse {source}: {exc}")
        return items

    # RSS: channel/item ; Atom: feed/entry
    entries = [e for e in root.iter() if _strip_ns(e.tag) in ("item", "entry")]
    for entry in entries[:limit]:
        title = ""
        link = ""
        summary = ""
        for child in entry:
            name = _strip_ns(child.tag)
            if name == "title" and child.text:
                title = child.text.strip()
            elif name == "link":
                # RSS puts the URL in text; Atom uses the href attribute.
                link = (child.text or child.get("href") or link or "").strip()
            elif name in ("description", "summary", "content") and not summary:
                summary = _clean_text(child.text or "")
        if title:
            items.append(FeedItem(source=source, title=title, url=link, summary=summary))
    return items


def scrape_page(source: str, url: str, limit: int = MAX_ITEMS_PER_SOURCE) -> list[FeedItem]:
    """Best-effort scrape of a headline/article page (no login required).

    Extraction is intentionally generic (links with substantial anchor text)
    so it degrades gracefully when these sites change their markup.
    """
    items: list[FeedItem] = []
    if BeautifulSoup is None:
        print(f"  ! beautifulsoup4 not installed, skipping {source}")
        return items
    try:
        raw = _http_get(url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"  ! could not fetch {source}: {exc}")
        return items

    soup = BeautifulSoup(raw, "html.parser")

    # Prefer real headings; fall back to substantial links.
    seen: set[str] = set()
    for tag in soup.find_all(["h1", "h2", "h3"]):
        title = _clean_text(tag.get_text(" "), limit=200)
        if not title or len(title) < 15 or title.lower() in seen:
            continue
        link = ""
        anchor = tag.find("a") or tag.find_parent("a")
        if anchor and anchor.get("href"):
            link = urllib.parse.urljoin(url, anchor["href"])
        seen.add(title.lower())
        items.append(FeedItem(source=source, title=title, url=link))
        if len(items) >= limit:
            break

    if not items:  # markup gave us nothing usable — fall back to links
        for anchor in soup.find_all("a"):
            title = _clean_text(anchor.get_text(" "), limit=200)
            if len(title) < 25 or title.lower() in seen:
                continue
            seen.add(title.lower())
            link = urllib.parse.urljoin(url, anchor.get("href", ""))
            items.append(FeedItem(source=source, title=title, url=link))
            if len(items) >= limit:
                break

    if not items:
        print(f"  ! no items extracted from {source} (markup may have changed)")
    return items


def fetch_insider_feeds() -> list[FeedItem]:
    """Stage 1: pull the latest content from the 4 AI-industry sources."""
    _print_header("STAGE 1 — fetch_insider_feeds()")
    all_items: list[FeedItem] = []

    for source, url in RSS_FEEDS:
        print(f"\n· {source} (RSS)")
        items = fetch_rss(source, url)
        for item in items:
            print(f"    - {item.title}")
        all_items.extend(items)

    for source, url in SCRAPE_SOURCES:
        print(f"\n· {source} (scrape)")
        items = scrape_page(source, url)
        for item in items:
            print(f"    - {item.title}")
        all_items.extend(items)

    print(f"\n  => {len(all_items)} insider items across {len(RSS_FEEDS) + len(SCRAPE_SOURCES)} sources")
    return all_items


# --------------------------------------------------------------------------- #
# Stage 2 — mainstream signal                                                 #
# --------------------------------------------------------------------------- #

def fetch_mainstream_signal() -> list[dict[str, str]]:
    """Stage 2: targeted web searches matched to EWAI's focus areas."""
    _print_header("STAGE 2 — fetch_mainstream_signal()")
    client = _client()
    results: list[dict[str, str]] = []

    for area, query in MAINSTREAM_QUERIES:
        print(f"\n· searching: {area}")
        prompt = (
            f"Search the web for {query}. "
            "Focus on what a smart but non-technical professional would find "
            "relevant. Return a short bulleted summary (3-5 bullets) of the most "
            "concrete, recent developments, each with the source name. "
            "Do not include preamble."
        )
        summary = _run_with_web_search(client, prompt)
        print(textwrap.indent(summary or "(no result)", "    "))
        results.append({"area": area, "query": query, "summary": summary})

    return results


# --------------------------------------------------------------------------- #
# Stage 3 — cross reference                                                    #
# --------------------------------------------------------------------------- #

CROSS_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "topics_in_both": {"type": "array", "items": {"type": "string"}},
        "mainstream_only": {"type": "array", "items": {"type": "string"}},
        "insider_only": {"type": "array", "items": {"type": "string"}},
        "winning_topic": {"type": "string"},
        "winning_reason": {"type": "string"},
    },
    "required": [
        "topics_in_both",
        "mainstream_only",
        "insider_only",
        "winning_topic",
        "winning_reason",
    ],
    "additionalProperties": False,
}


def cross_reference(
    insider_results: list[FeedItem],
    mainstream_results: list[dict[str, str]],
) -> dict[str, Any]:
    """Stage 3: Claude compares the two signal sources and picks a topic."""
    _print_header("STAGE 3 — cross_reference()")
    client = _client()

    insider_block = "\n".join(f"- {i.as_line()}" for i in insider_results) or "(none)"
    mainstream_block = "\n\n".join(
        f"## {r['area']}\n{r['summary']}" for r in mainstream_results
    ) or "(none)"

    prompt = textwrap.dedent(
        f"""\
        You are curating a weekly AI topic for the EWAI community.

        {EWAI_MISSION}

        Here is what AI INSIDER sources published this week:
        {insider_block}

        Here is the MAINSTREAM signal from targeted web searches:
        {mainstream_block}

        Cross-reference these two sets. Identify:
        - topics_in_both: topics appearing in BOTH insider and mainstream sources
        - mainstream_only: topics only in the mainstream signal
        - insider_only: topics only in the insider feeds
        Then choose ONE winning_topic for this week's message and explain
        winning_reason in 2-3 sentences.

        Selection weighting:
        {SELECTION_WEIGHTING}
        """
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": CROSS_REFERENCE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
        **_thinking_kwargs(),
    )
    text = _text_from_response(response)
    import json

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"winning_topic": text, "winning_reason": "", "topics_in_both": [],
                "mainstream_only": [], "insider_only": []}

    def _show(label: str, items: list[str]) -> None:
        print(f"\n{label}:")
        for it in items or ["(none)"]:
            print(f"    - {it}")

    _show("Topics in BOTH (strongest)", data.get("topics_in_both", []))
    _show("Mainstream-only", data.get("mainstream_only", []))
    _show("Insider-only", data.get("insider_only", []))
    print(f"\n>>> WINNING TOPIC: {data.get('winning_topic', '')}")
    print(f"    why: {data.get('winning_reason', '')}")
    return data


# --------------------------------------------------------------------------- #
# Stage 4 — deep dive                                                          #
# --------------------------------------------------------------------------- #

def deep_dive(topic: str) -> str:
    """Stage 4: Claude + web search gather details on the winning topic."""
    _print_header("STAGE 4 — deep_dive()")
    print(f"topic: {topic}\n")
    client = _client()

    prompt = textwrap.dedent(
        f"""\
        Research this topic in depth using web search, pulling from multiple
        sources: "{topic}"

        {EWAI_MISSION}

        Produce a briefing that covers:
        1. What is actually happening (plain language, no jargon).
        2. Why it matters to women engaged with AI — whether that's a direct
           impact on career, financial, or daily-life decisions, or simply
           something significant for understanding where AI is heading.
        3. If a natural, concrete action exists, name one or two realistic
           things a member could DO about it. If the topic is more about
           staying informed than personal action, say so instead of forcing one.
        Cite the source names you draw from. Keep it under 350 words.
        """
    )
    research = _run_with_web_search(client, prompt, max_uses=6)
    print(textwrap.indent(research or "(no result)", "  "))
    return research


# --------------------------------------------------------------------------- #
# Stage 5 — WhatsApp message                                                   #
# --------------------------------------------------------------------------- #

def generate_whatsapp_message(research: str) -> str:
    """Stage 5: reframe the research through EWAI's mission and voice."""
    _print_header("STAGE 5 — generate_whatsapp_message()")
    client = _client()

    prompt = textwrap.dedent(
        f"""\
        {EWAI_MISSION}

        EWAI brand voice: {EWAI_VOICE}.

        Using the research below, write a WhatsApp message for the EWAI
        community. Requirements:
        - Reframe the topic through EWAI's mission (practical AI adoption for
          women navigating career/financial/life inflection points).
        - Structure: (1) the topic, (2) why it matters to us, (3) ONE concrete
          action step she can take this week — ONLY if one naturally exists.
          If the topic is more about staying informed than personal action,
          close on why it matters instead of forcing an action step.
        Short enough for WhatsApp: aim for ~120 words. Warm, purposeful,
          courageous. A couple of tasteful emoji are fine. No hashtags,
          no markdown headers.

        RESEARCH:
        {research}
        """
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
        **_thinking_kwargs(),
    )
    message = _text_from_response(response)
    print("\n" + message + "\n")
    return message


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #

def run_pipeline() -> DigestResult:
    result = DigestResult()
    result.insider = fetch_insider_feeds()
    result.mainstream = fetch_mainstream_signal()
    result.cross_reference = cross_reference(result.insider, result.mainstream)
    topic = result.cross_reference.get("winning_topic", "")
    if not topic:
        sys.exit("No winning topic was selected; stopping before deep dive.")
    result.deep_dive = deep_dive(topic)
    result.whatsapp_message = generate_whatsapp_message(result.deep_dive)

    _print_header("DONE — final WhatsApp message")
    print(result.whatsapp_message)
    return result


def main() -> None:
    run_pipeline()


if __name__ == "__main__":
    main()
