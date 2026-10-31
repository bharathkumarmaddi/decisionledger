# 🎙️ DecisionLedger: Speak it. Remember it. Ship it.

**Product Space x Wispr Flow Agentic AI Hackathon '26 · Problem 03: Meeting-to-Execution Agent**

Decisions made in meetings get lost. DecisionLedger turns messy spoken meetings (captured with Wispr Flow) into owned, dated tasks, remembers every decision across meetings, and **catches when a new decision contradicts an old one**.

## What the agent does
1. **Voice in:** speak the standup with Wispr Flow. Fillers, Hinglish and self-corrections ("Ravi does it, no wait, Arjun does") are cleaned, keeping the corrected version.
2. **Transcript ingestion with speaker attribution** (`Name:` lines, or inferred).
3. **Extraction** of decisions, action items, owners and deadlines (relative dates resolved) with Gemini, as JSON.
4. **Conflict radar:** compares new decisions with the cross-meeting decision memory (SQLite) and flags contradictions; you choose override or keep.
5. **Confirmation step:** an editable review table; nothing is saved or posted until you confirm.
6. **Execution via integrations:** Google Calendar event links, `.ics` export with 1-day-before reminders, and a Slack/Discord webhook post.
7. **Reminders** for tasks due within 48 hours and overdue ones.
8. **Weekly digest per person.**
9. **Voice follow-ups:** "move Ravi's migration to Thursday", "mark the deck done", "draft an email to the team".

## Run
```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your_google_ai_studio_key
streamlit run app.py
```
Streamlit Cloud: add `GEMINI_API_KEY` under Secrets. With no key it runs in a limited offline demo mode.

## Demo flow
Load sample meeting 1 → Run → Confirm. Then load sample meeting 2 (switches Firebase to Supabase, moves demo day) → Run → conflict radar fires → Confirm → calendar links and digest.

## Stack
Streamlit · Gemini 2.5 Flash (Google AI Studio, `google-genai`) · SQLite · Google Calendar · webhooks · Wispr Flow for voice input and for building.
