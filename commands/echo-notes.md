Fetch AI Companion notes for a Zoom meeting the user hosted.

If the user provided a meeting UUID or numeric ID as arguments, call the `ai_meeting_notes` tool from the ECHO MCP server with it directly. If they gave a topic or date instead (or nothing), call `list_past_meetings` first, identify the meeting they mean, then call `ai_meeting_notes` with its UUID. Present the notes with the summary sections and next steps clearly formatted. Note: Zoom only exposes AI Companion notes to the meeting host, so meetings the user merely attended will not be available.
