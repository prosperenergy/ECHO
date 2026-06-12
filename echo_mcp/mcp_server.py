"""ECHO — Explore Calls, Hearings & Observations.

MCP server for searching Zoom meeting transcripts.
Part of the Alpine Toolkit.
"""

from __future__ import annotations

import functools
import re
from datetime import date, timedelta

import httpx
from mcp.server.fastmcp import FastMCP

from .auth import AuthServiceUnreachable, load_tokens, tokens_valid
from .connector import NotConfiguredError, ZoomConnector


def _graceful(fn):
    """Decorator that turns common setup/connectivity errors into a friendly
    message for the AI client instead of a stack trace."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except AuthServiceUnreachable as e:
            return (
                "**ECHO needs VPN access to refresh your session.**\n\n"
                f"{e}\n\n"
                "Connect to the Percona VPN and try again — it should take "
                "less than a second once you're back on."
            )
        except NotConfiguredError as e:
            return "**ECHO is not set up yet.**\n\n{}\n\nRun `auth_status` for details.".format(e)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 401, 403):
                return (
                    "**Zoom rejected the request ({}).**\n\n"
                    "Your saved token may be missing a newly added scope "
                    "(e.g. `meeting:read:summary` for AI Companion notes). "
                    "Make sure the scope is enabled on the Zoom Marketplace "
                    "app, then run `echo-login` to re-authenticate.\n\n"
                    "Zoom said: {}".format(e.response.status_code, e.response.text[:300])
                )
            raise

    return wrapper

mcp = FastMCP(
    "ECHO",
    instructions="Explore Calls, Hearings & Observations — search Zoom meeting transcripts",
)
zoom = ZoomConnector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_vtt(vtt_text: str) -> list[dict]:
    """Parse a WebVTT transcript into a list of {timestamp, speaker, text} dicts."""
    # Normalize line endings — Zoom serves CRLF, our regex expects LF
    vtt_text = vtt_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\n+", vtt_text.strip())
    entries = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines or lines[0].startswith("WEBVTT"):
            continue
        ts_line = None
        text_lines = []
        for line in lines:
            if "-->" in line:
                ts_line = line.strip()
            elif ts_line is not None:
                text_lines.append(line.strip())
        if ts_line and text_lines:
            full_text = " ".join(text_lines)
            speaker = ""
            text = full_text
            if ": " in full_text:
                speaker, text = full_text.split(": ", 1)
            entries.append({"timestamp": ts_line, "speaker": speaker, "text": text})
    return entries


async def _get_transcript_for_meeting(meeting_id: str) -> list[dict] | None:
    """Fetch and parse the transcript for a meeting, or None if unavailable.

    Uses /users/me/recordings (which our scopes cover) and scans for the
    matching meeting. Avoids /meetings/{id}/recordings — that endpoint
    requires a different scope and returns 400 with our user-managed
    token.
    """
    # Search last 30 days (Zoom's max range per request). Should be
    # enough — transcripts older than that are rarely interesting and
    # we'd need a wider search strategy anyway.
    to_date = date.today()
    from_date = to_date - timedelta(days=30)
    data = await zoom.list_recordings(
        from_date=from_date.isoformat(), to_date=to_date.isoformat(), page_size=300
    )

    target = None
    for meeting in data.get("meetings", []):
        mid = str(meeting.get("id") or "")
        muuid = str(meeting.get("uuid") or "")
        if meeting_id == mid or meeting_id == muuid:
            target = meeting
            break

    if target is None:
        return None

    download_url = None
    for f in target.get("recording_files", []):
        if (
            f.get("recording_type") == "audio_transcript"
            or f.get("file_extension", "").upper() == "VTT"
            or f.get("file_type") == "TRANSCRIPT"
        ):
            download_url = f.get("download_url")
            break

    if not download_url:
        return None

    vtt_text = await zoom.get_transcript_content(download_url)
    return _parse_vtt(vtt_text)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def auth_status() -> str:
    """Check if ECHO is authenticated with Zoom.

    Returns the current auth status and instructions if not logged in.
    """
    try:
        _ = zoom.client_id  # Triggers lazy resolution
    except NotConfiguredError as e:
        return f"**ECHO is not configured yet.**\n\n{e}"

    tokens = load_tokens()
    if tokens is None:
        return (
            "**Not authenticated.**\n\n"
            "Run this in your terminal to log in:\n"
            "```\necho-login\n```\n"
            "This will open Zoom in your browser to authorize ECHO."
        )

    if tokens_valid(tokens):
        return "**Authenticated** - ECHO is connected to your Zoom account."
    else:
        return (
            "**Token expired** - ECHO will auto-refresh on the next request.\n"
            "If that fails, run `echo-login` to re-authenticate."
        )


@mcp.tool()
@_graceful
async def list_meetings(days: int = 30) -> str:
    """List recent Zoom meetings that have cloud recordings (and potentially transcripts).

    Args:
        days: How many days back to look (default 30, max 30).
    """
    days = min(days, 30)
    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    data = await zoom.list_recordings(
        from_date=from_date.isoformat(), to_date=to_date.isoformat()
    )

    meetings = data.get("meetings", [])
    if not meetings:
        return "No recordings found in the last {} days.".format(days)

    results = []
    for m in meetings:
        has_transcript = any(
            f.get("recording_type") == "audio_transcript"
            or f.get("file_extension", "").upper() == "VTT"
            for f in m.get("recording_files", [])
        )
        results.append(
            "- **{}** (ID: {}) — {} | Transcript: {}".format(
                m.get("topic", "Untitled"),
                m.get("id") or m.get("uuid"),
                m.get("start_time", "unknown date"),
                "Yes" if has_transcript else "No",
            )
        )

    return "## Meetings with recordings (last {} days)\n\n{}".format(
        days, "\n".join(results)
    )


@mcp.tool()
@_graceful
async def get_transcript(meeting_id: str) -> str:
    """Get the full transcript for a specific Zoom meeting.

    Args:
        meeting_id: The Zoom meeting ID (numeric or UUID).
    """
    entries = await _get_transcript_for_meeting(meeting_id)
    if entries is None:
        return "No transcript found for meeting {}.".format(meeting_id)

    lines = []
    for e in entries:
        speaker = "**{}**".format(e["speaker"]) if e["speaker"] else ""
        lines.append("{} {}: {}".format(e["timestamp"], speaker, e["text"]))

    return "## Transcript for meeting {}\n\n{}".format(meeting_id, "\n".join(lines))


@mcp.tool()
@_graceful
async def search_transcripts(query: str, days: int = 30) -> str:
    """Search across Zoom meeting transcripts for a keyword or phrase.

    Fetches all recent meetings with transcripts and searches through them.

    Args:
        query: The search term or phrase to find in transcripts.
        days: How many days back to search (default 30, max 30).
    """
    days = min(days, 30)
    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    data = await zoom.list_recordings(
        from_date=from_date.isoformat(), to_date=to_date.isoformat()
    )

    meetings = data.get("meetings", [])
    if not meetings:
        return "No recordings found in the last {} days.".format(days)

    query_lower = query.lower()
    results = []

    for m in meetings:
        meeting_id = str(m.get("id") or m.get("uuid"))
        topic = m.get("topic", "Untitled")

        entries = await _get_transcript_for_meeting(meeting_id)
        if not entries:
            continue

        matches = [e for e in entries if query_lower in e["text"].lower()]
        if matches:
            hit_lines = []
            for e in matches[:10]:
                speaker = e["speaker"] or "Unknown"
                hit_lines.append(
                    "  - [{}] {}: ...{}...".format(
                        e["timestamp"].split(" --> ")[0],
                        speaker,
                        e["text"][:200],
                    )
                )
            results.append(
                "### {} (ID: {})\n{} match(es)\n{}".format(
                    topic, meeting_id, len(matches), "\n".join(hit_lines)
                )
            )

    if not results:
        return 'No matches for "{}" in the last {} days of transcripts.'.format(
            query, days
        )

    return '## Search results for "{}"\n\n{}'.format(query, "\n\n".join(results))


@mcp.tool()
@_graceful
async def meeting_summary(meeting_id: str) -> str:
    """Get a structured summary of a Zoom meeting transcript.

    Extracts participants and a condensed view of the conversation flow.

    Args:
        meeting_id: The Zoom meeting ID (numeric or UUID).
    """
    entries = await _get_transcript_for_meeting(meeting_id)
    if entries is None:
        return "No transcript found for meeting {}.".format(meeting_id)

    speakers = sorted({e["speaker"] for e in entries if e["speaker"]})

    segments = []
    current_speaker = None
    current_texts = []
    for e in entries:
        if e["speaker"] != current_speaker:
            if current_speaker and current_texts:
                combined = " ".join(current_texts)
                if len(combined) > 300:
                    combined = combined[:300] + "..."
                segments.append("**{}**: {}".format(current_speaker, combined))
            current_speaker = e["speaker"]
            current_texts = [e["text"]]
        else:
            current_texts.append(e["text"])

    if current_speaker and current_texts:
        combined = " ".join(current_texts)
        if len(combined) > 300:
            combined = combined[:300] + "..."
        segments.append("**{}**: {}".format(current_speaker, combined))

    duration_note = ""
    if entries:
        first_ts = entries[0]["timestamp"].split(" --> ")[0]
        last_ts = entries[-1]["timestamp"].split(" --> ")[0]
        duration_note = "Time span: {} to {}".format(first_ts, last_ts)

    return (
        "## Meeting summary ({})\n\n"
        "**Participants:** {}\n{}\n\n"
        "### Conversation flow\n\n{}"
    ).format(
        meeting_id,
        ", ".join(speakers) if speakers else "Unknown",
        duration_note,
        "\n\n".join(segments),
    )


def _format_summary_section(details, next_steps) -> str:
    """Render summary_details + next_steps from a Zoom AI Companion payload."""
    parts = []
    if isinstance(details, str) and details.strip():
        parts.append(details.strip())
    elif isinstance(details, list):
        for d in details:
            label = (d or {}).get("label", "")
            body = (d or {}).get("summary", "")
            if label:
                parts.append("**{}**\n{}".format(label, body))
            elif body:
                parts.append(body)
    if next_steps:
        steps = "\n".join("- {}".format(s) for s in next_steps if s)
        if steps:
            parts.append("**Next steps**\n{}".format(steps))
    return "\n\n".join(parts)


@mcp.tool()
@_graceful
async def list_past_meetings(days: int = 30) -> str:
    """List recent Zoom meetings you hosted, including ones without cloud recordings.

    Use this to find meetings that only used AI Companion notes (the Zoom
    note-taking feature) — they don't appear in `list_meetings`, which only
    covers cloud recordings. Feed a meeting's UUID to `ai_meeting_notes`.

    Args:
        days: How many days back to look (default 30).
    """
    data = await zoom.list_past_meetings(page_size=300)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    meetings = [
        m for m in data.get("meetings", [])
        if m.get("start_time", "")[:10] >= cutoff
    ]
    if not meetings:
        return "No past hosted meetings found in the last {} days.".format(days)

    meetings.sort(key=lambda m: m.get("start_time", ""), reverse=True)
    results = []
    for m in meetings:
        results.append(
            "- **{}** — {} | ID: {} | UUID: {}".format(
                m.get("topic", "Untitled"),
                m.get("start_time", "unknown date"),
                m.get("id", "?"),
                m.get("uuid", "?"),
            )
        )

    return (
        "## Past hosted meetings (last {} days)\n\n{}\n\n"
        "Use `ai_meeting_notes` with a UUID to fetch AI Companion notes."
    ).format(days, "\n".join(results))


@mcp.tool()
@_graceful
async def ai_meeting_notes(meeting_id: str) -> str:
    """Get the AI Companion (Zoom note-taking) summary for a meeting you hosted.

    Works even when the meeting was not cloud-recorded — this reads the
    summary Zoom's AI Companion generated, not the recording transcript.
    Zoom only exposes these to the meeting host: notes from meetings you
    merely attended are not accessible.

    Args:
        meeting_id: The Zoom meeting UUID (preferred) or numeric meeting ID.
            For recurring meetings the numeric ID returns the latest instance.
    """
    data = await zoom.get_meeting_summary(meeting_id)
    if data is None:
        return (
            "No AI Companion summary found for meeting {}.\n\n"
            "This usually means one of:\n"
            "- AI Companion meeting summary wasn't turned on for that meeting\n"
            "- You weren't the host (Zoom only exposes summaries to the host)\n"
            "- The summary is still being generated (can take a few minutes "
            "after the meeting ends)".format(meeting_id)
        )

    title = data.get("summary_title") or data.get("meeting_topic") or "Meeting"
    header_bits = []
    if data.get("meeting_start_time"):
        header_bits.append("Started: {}".format(data["meeting_start_time"]))
    if data.get("meeting_host_email"):
        header_bits.append("Host: {}".format(data["meeting_host_email"]))

    # Prefer the host-edited summary when it exists
    edited = data.get("edited_summary") or {}
    body = _format_summary_section(
        edited.get("summary_details"), edited.get("next_steps")
    )
    if not body:
        body = _format_summary_section(
            data.get("summary_details") or data.get("summary_overview"),
            data.get("next_steps"),
        )
    if not body:
        body = "_Summary exists but has no content yet — try again shortly._"

    return "## AI Companion notes: {}\n\n{}\n\n{}".format(
        title, " | ".join(header_bits), body
    )


# ---------------------------------------------------------------------------
# Prompts (slash commands in MCP-compatible clients)
# ---------------------------------------------------------------------------
#
# Every MCP client that supports prompts (Claude Desktop, Claude Code,
# Cowork) exposes these as slash commands automatically. They are thin
# wrappers that instruct the assistant to call the right tool and format
# the output well.


@mcp.prompt()
def echo_status() -> str:
    """Check your ECHO authentication and connectivity status."""
    return (
        "Use the `auth_status` tool to check whether ECHO is authenticated "
        "with Zoom. Report the status concisely and, if not authenticated, "
        "tell the user exactly what to do next."
    )


@mcp.prompt()
def echo_recent(days: str = "30") -> str:
    """List recent Zoom meetings with cloud recordings.

    Args:
        days: How many days back to look (default 30, max 30).
    """
    return (
        f"Use the `list_meetings` tool with days={days} to show recent Zoom "
        f"meetings. Format as a concise list with meeting title, date, and "
        f"meeting ID. Highlight which ones have transcripts available."
    )


@mcp.prompt()
def echo_search(query: str) -> str:
    """Search your Zoom meeting transcripts for a keyword or phrase.

    Args:
        query: The word or phrase to search for.
    """
    return (
        f"Use the `search_transcripts` tool to find transcripts mentioning "
        f'"{query}" across the last 30 days of Zoom meetings. Format the '
        f"results grouped by meeting, with the matching quotes and who said "
        f"them. If nothing is found, say so plainly."
    )


@mcp.prompt()
def echo_summary(meeting_id: str) -> str:
    """Summarize a specific Zoom meeting.

    Args:
        meeting_id: The Zoom meeting ID (numeric or UUID) to summarize.
    """
    return (
        f"Use the `meeting_summary` tool for meeting {meeting_id} to get the "
        f"participants and conversation flow. Then produce a concise "
        f"executive summary: key decisions, action items, and open questions. "
        f"Attribute action items to specific people."
    )


@mcp.prompt()
def echo_notes(meeting: str = "") -> str:
    """Fetch AI Companion notes for a Zoom meeting you hosted.

    Args:
        meeting: Meeting UUID/ID, or a topic/date hint to find it (optional).
    """
    if meeting:
        return (
            f"The user wants the Zoom AI Companion notes for: {meeting}. "
            f"If that looks like a meeting UUID or numeric ID, call the "
            f"`ai_meeting_notes` tool with it directly. Otherwise call "
            f"`list_past_meetings` first, pick the meeting matching the "
            f"description, then call `ai_meeting_notes` with its UUID. "
            f"Present the notes with the summary sections and next steps "
            f"clearly formatted."
        )
    return (
        "Use the `list_past_meetings` tool to show the user's recent hosted "
        "meetings, then ask which one they want AI Companion notes for, and "
        "fetch it with `ai_meeting_notes`."
    )


@mcp.prompt()
def echo_transcript(meeting_id: str) -> str:
    """Fetch the full transcript of a Zoom meeting.

    Args:
        meeting_id: The Zoom meeting ID (numeric or UUID).
    """
    return (
        f"Use the `get_transcript` tool for meeting {meeting_id} and display "
        f"the full transcript. Preserve speaker attribution and timestamps. "
        f"If the meeting does not have a transcript, say so."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
