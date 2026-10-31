"""DecisionLedger - voice-first Meeting-to-Execution agent.

Speak a messy meeting/standup (via Wispr Flow) -> clean structured decisions,
action items, owners, deadlines -> conflict radar against earlier decisions ->
human confirmation -> tasks pushed to Google Calendar / webhook tracker ->
reminders + weekly digest per person.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

import streamlit as st

DB_PATH = os.environ.get("DL_DB", "decisionledger.db")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

SAMPLE_1 = """Priya: Okay so, um, quick standup. We decided we're using Firebase for the login, that's final.
Ravi: Yeah and uh I'll do the landing page, no wait, Arjun does the landing page, I'll do the database schema, by Monday.
Arjun: Fine, landing page by Wednesday. Also we said the demo day is November 10th.
Priya: And I'll write the pitch deck by Friday, like, basically the first draft."""

SAMPLE_2 = """Ravi: So, uh, update. I think we should actually switch to Supabase for auth, Firebase is annoying.
Priya: Okay, decided, Supabase then. Ravi you migrate it by Tuesday.
Arjun: Also, um, demo day moved, it's November 14th now, not the 10th.
Priya: Arjun, can you also record the demo video, say by the 12th?"""


# ---------------------------------------------------------------- storage
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS meetings(
        id INTEGER PRIMARY KEY, title TEXT, held_on TEXT, transcript TEXT, summary TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS decisions(
        id INTEGER PRIMARY KEY, meeting_id INTEGER, topic TEXT, decision TEXT,
        decided_by TEXT, held_on TEXT, status TEXT DEFAULT 'active')""")
    con.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY, meeting_id INTEGER, task TEXT, owner TEXT,
        deadline TEXT, status TEXT DEFAULT 'open')""")
    con.commit()
    return con


def active_decisions(con):
    rows = con.execute("SELECT id, topic, decision, decided_by, held_on FROM decisions "
                       "WHERE status='active' ORDER BY id").fetchall()
    return [dict(zip(["id", "topic", "decision", "decided_by", "held_on"], r)) for r in rows]


# ---------------------------------------------------------------- LLM
def gemini_json(prompt: str, api_key: str) -> dict:
    from google import genai  # google-genai
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=MODEL, contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0.2})
    text = resp.text.strip()
    text = re.sub(r"^```(json)?|```$", "", text).strip()
    return json.loads(text)


EXTRACT_PROMPT = """You are DecisionLedger, an agent that turns messy spoken meeting speech into
clean, structured execution data. The input came from voice dictation (Wispr Flow):
it can contain filler words (um, uh, like, basically), digressions, Hinglish, and
SELF-CORRECTIONS ("Ravi does it, no wait, Arjun does it"). Always keep the CORRECTED
version and drop the retracted one.

Today's date is {today}. Resolve relative dates ("Monday", "by Friday", "the 12th")
to absolute YYYY-MM-DD dates in the future.

PAST ACTIVE DECISIONS (team memory):
{memory}

TRANSCRIPT:
\"\"\"{transcript}\"\"\"

Return ONLY JSON with this shape:
{{
 "clean_transcript": [{{"speaker": "...", "text": "cleaned sentence without fillers"}}],
 "summary": "2 sentence summary",
 "decisions": [{{"topic": "short topic key e.g. 'auth provider'", "decision": "...", "decided_by": "..."}}],
 "action_items": [{{"task": "...", "owner": "...", "deadline": "YYYY-MM-DD or null"}}],
 "self_corrections": [{{"original": "...", "corrected_to": "..."}}],
 "conflicts": [{{"past_decision_id": <int>, "past": "...", "new": "...", "explanation": "..."}}]
}}
A conflict exists only when a new decision contradicts or changes a PAST ACTIVE DECISION
on the same topic. If speakers are not labelled, infer them from context or use "Unknown"."""


def mock_extract(transcript: str, memory: list) -> dict:
    """Offline fallback so the UI can be demoed without a key (rule-based, limited)."""
    lines = [l for l in transcript.splitlines() if ":" in l]
    clean, actions, decisions, corrections = [], [], [], []
    for l in lines:
        spk, txt = l.split(":", 1)
        m = re.search(r"(\w+) does ([^,]+), no wait, (\w+) does", txt)
        if m:
            corrections.append({"original": f"{m.group(1)} does {m.group(2)}",
                                "corrected_to": f"{m.group(3)} does {m.group(2)}"})
        t = re.sub(r"\b(um|uh|like|basically|so|okay|yeah)\b,?\s*", "", txt, flags=re.I).strip()
        clean.append({"speaker": spk.strip(), "text": t})
        if re.search(r"decided|final", txt, re.I):
            topic = "auth provider" if re.search(r"firebase|supabase", txt, re.I) else "general"
            decisions.append({"topic": topic, "decision": t, "decided_by": spk.strip()})
    conflicts = []
    for d in decisions:
        for p in memory:
            if p["topic"] == d["topic"] and p["decision"] != d["decision"]:
                conflicts.append({"past_decision_id": p["id"], "past": p["decision"],
                                  "new": d["decision"], "explanation": "Same topic, different decision."})
    return {"clean_transcript": clean, "summary": "Offline demo mode (add a Gemini key for full AI).",
            "decisions": decisions, "action_items": actions,
            "self_corrections": corrections, "conflicts": conflicts}


# ---------------------------------------------------------------- integrations
def gcal_link(task: str, owner: str, deadline: str) -> str:
    d = dt.date.fromisoformat(deadline)
    params = {"action": "TEMPLATE", "text": f"[{owner}] {task}",
              "dates": f"{d:%Y%m%d}/{d + dt.timedelta(days=1):%Y%m%d}",
              "details": "Created by DecisionLedger"}
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


def ics_for(tasks: list) -> str:
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DecisionLedger//EN"]
    for i, t in enumerate(tasks):
        if not t.get("deadline"):
            continue
        d = dt.date.fromisoformat(t["deadline"])
        out += ["BEGIN:VEVENT", f"UID:dl-{int(time.time())}-{i}@decisionledger",
                f"DTSTAMP:{dt.datetime.utcnow():%Y%m%dT%H%M%SZ}",
                f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + dt.timedelta(days=1):%Y%m%d}",
                f"SUMMARY:[{t['owner']}] {t['task']}",
                "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
                f"DESCRIPTION:Due tomorrow: {t['task']}", "END:VALARM", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(out)


def post_webhook(url: str, text: str) -> str:
    # Works with Slack / Discord incoming webhooks (and most trackers' webhook triggers).
    body = {"text": text, "content": text}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return f"HTTP {r.status}"


# ---------------------------------------------------------------- UI
st.set_page_config(page_title="DecisionLedger", page_icon="🎙️", layout="wide")
con = db()

with st.sidebar:
    st.title("🎙️ DecisionLedger")
    st.caption("Speak it. Remember it. Ship it.")
    api_key = st.text_input("Gemini API key (Google AI Studio)", type="password",
                            value=os.environ.get("GEMINI_API_KEY", ""))
    if not api_key:
        try:
            api_key = st.secrets.get("GEMINI_API_KEY", "")
        except Exception:
            api_key = ""
    webhook = st.text_input("Slack/Discord webhook (optional tracker integration)")
    st.markdown("---")
    st.markdown("**Voice input:** click the transcript box and hold your Wispr Flow hotkey. "
                "Speak naturally: fillers and corrections are handled.")
    if st.button("Reset team memory"):
        for t in ("meetings", "decisions", "tasks"):
            con.execute(f"DELETE FROM {t}")
        con.commit()
        st.session_state.pop("result", None)
        st.success("Memory cleared.")

tab_meet, tab_follow, tab_mem, tab_digest = st.tabs(
    ["🗣️ New meeting", "🔁 Voice follow-up", "🧠 Decision memory", "📬 Reminders & digest"])

# -------- New meeting
with tab_meet:
    c1, c2 = st.columns([3, 1])
    with c1:
        title = st.text_input("Meeting title", "Team standup")
    with c2:
        held_on = st.date_input("Held on", dt.date.today())
    s1, s2, _ = st.columns([1, 1, 4])
    if s1.button("Load sample meeting 1"):
        st.session_state["transcript"] = SAMPLE_1
    if s2.button("Load sample meeting 2"):
        st.session_state["transcript"] = SAMPLE_2
    transcript = st.text_area("Speak or paste the meeting (use 'Name:' per line for speaker attribution)",
                              key="transcript", height=180)

    if st.button("▶ Run agent", type="primary", disabled=not transcript.strip()):
        memory = active_decisions(con)
        status = st.status("Agent working…", expanded=True)
        t0 = time.time()
        status.write("🧹 Cleaning speech: removing fillers, applying self-corrections…")
        status.write(f"🧠 Loading team memory: {len(memory)} active past decisions…")
        try:
            if api_key:
                prompt = EXTRACT_PROMPT.format(today=held_on.isoformat(),
                                               memory=json.dumps(memory, indent=1) or "[]",
                                               transcript=transcript)
                result = gemini_json(prompt, api_key)
            else:
                result = mock_extract(transcript, memory)
        except Exception as e:
            status.update(label="Agent failed", state="error")
            st.error(f"Model error: {e}")
            st.stop()
        status.write(f"📋 Extracted {len(result.get('decisions', []))} decisions and "
                     f"{len(result.get('action_items', []))} action items")
        status.write(f"⚠️ Conflict radar: {len(result.get('conflicts', []))} conflict(s) found")
        status.update(label=f"Done in {time.time() - t0:.1f}s", state="complete", expanded=False)
        st.session_state["result"] = result
        st.session_state["meta"] = {"title": title, "held_on": held_on.isoformat(), "transcript": transcript}

    res = st.session_state.get("result")
    if res:
        left, right = st.columns(2)
        with left:
            st.subheader("Clean transcript")
            for line in res.get("clean_transcript", []):
                st.markdown(f"**{line.get('speaker', '?')}:** {line.get('text', '')}")
            if res.get("self_corrections"):
                st.subheader("Self-corrections applied")
                for c in res["self_corrections"]:
                    st.markdown(f"~~{c.get('original')}~~ → **{c.get('corrected_to')}**")
            st.info(res.get("summary", ""))
        with right:
            if res.get("conflicts"):
                st.subheader("⚠️ Conflict radar")
                for i, c in enumerate(res["conflicts"]):
                    st.error(f"**Earlier:** {c.get('past')}\n\n**Now:** {c.get('new')}\n\n{c.get('explanation', '')}")
                    st.radio("Resolve", ["New decision overrides old", "Keep old decision"],
                             key=f"conf_{i}", horizontal=True)
            st.subheader("Decisions")
            for d in res.get("decisions", []):
                st.markdown(f"✅ **{d.get('topic')}**: {d.get('decision')} _(by {d.get('decided_by')})_")

        st.subheader("Review action items before anything is posted")
        items = res.get("action_items", [])
        edited = st.data_editor(
            [{"approve": True, "task": a.get("task"), "owner": a.get("owner"),
              "deadline": a.get("deadline") or ""} for a in items] or
            [{"approve": False, "task": "", "owner": "", "deadline": ""}],
            num_rows="dynamic", width="stretch", key="editor")

        if st.button("✅ Confirm & execute", type="primary"):
            meta = st.session_state["meta"]
            cur = con.execute("INSERT INTO meetings(title, held_on, transcript, summary) VALUES(?,?,?,?)",
                              (meta["title"], meta["held_on"], meta["transcript"], res.get("summary", "")))
            mid = cur.lastrowid
            for i, c in enumerate(res.get("conflicts", [])):
                if st.session_state.get(f"conf_{i}", "").startswith("New") and c.get("past_decision_id"):
                    con.execute("UPDATE decisions SET status='superseded' WHERE id=?", (c["past_decision_id"],))
            for d in res.get("decisions", []):
                con.execute("INSERT INTO decisions(meeting_id, topic, decision, decided_by, held_on) "
                            "VALUES(?,?,?,?,?)", (mid, d.get("topic"), d.get("decision"),
                                                  d.get("decided_by"), meta["held_on"]))
            approved = [r for r in edited if r.get("approve") and r.get("task")]
            for r in approved:
                con.execute("INSERT INTO tasks(meeting_id, task, owner, deadline) VALUES(?,?,?,?)",
                            (mid, r["task"], r["owner"], r["deadline"] or None))
            con.commit()
            st.success(f"Saved to memory: {len(res.get('decisions', []))} decisions, {len(approved)} tasks.")
            dated = [r for r in approved if r.get("deadline")]
            for r in dated:
                try:
                    st.markdown(f"📅 [{r['owner']}: {r['task']} (due {r['deadline']})]"
                                f"({gcal_link(r['task'], r['owner'], r['deadline'])})")
                except ValueError:
                    pass
            if dated:
                st.download_button("⬇ Download all tasks (.ics with 1-day reminders)",
                                   ics_for(dated), "decisionledger_tasks.ics", "text/calendar")
            if webhook and approved:
                msg = f"*{meta['title']}* tasks:\n" + "\n".join(
                    f"• {r['owner']}: {r['task']} (due {r['deadline'] or 'n/a'})" for r in approved)
                try:
                    st.info(f"Posted to tracker webhook: {post_webhook(webhook, msg)}")
                except Exception as e:
                    st.warning(f"Webhook failed: {e}")

# -------- Voice follow-up
with tab_follow:
    st.markdown("Speak a follow-up about your tasks, e.g. *\"move Ravi's migration to Thursday\"* "
                "or *\"mark the pitch deck done\"* or *\"draft an email to the team with this week's tasks\"*.")
    cmd = st.text_area("Follow-up command", key="cmd", height=90)
    if st.button("▶ Run follow-up", disabled=not (cmd.strip() and api_key)):
        rows = con.execute("SELECT id, task, owner, deadline, status FROM tasks").fetchall()
        tasks = [dict(zip(["id", "task", "owner", "deadline", "status"], r)) for r in rows]
        prompt = f"""Today is {dt.date.today()}. Current tasks: {json.dumps(tasks)}.
Spoken command (may contain fillers/corrections): "{cmd}".
Return JSON: {{"updates":[{{"id":<int>,"deadline":"YYYY-MM-DD or null","status":"open|done or null"}}],
"email_draft": "text or empty", "reply": "one sentence confirming what you did"}}"""
        try:
            out = gemini_json(prompt, api_key)
            for u in out.get("updates", []):
                if u.get("deadline"):
                    con.execute("UPDATE tasks SET deadline=? WHERE id=?", (u["deadline"], u["id"]))
                if u.get("status"):
                    con.execute("UPDATE tasks SET status=? WHERE id=?", (u["status"], u["id"]))
            con.commit()
            st.success(out.get("reply", "Done."))
            if out.get("email_draft"):
                st.text_area("Email draft (review before sending)", out["email_draft"], height=200)
        except Exception as e:
            st.error(f"Model error: {e}")
    if not api_key:
        st.caption("Add your Gemini key in the sidebar to use follow-ups.")

# -------- Memory
with tab_mem:
    st.subheader("Decision memory across meetings")
    rows = con.execute("SELECT held_on, topic, decision, decided_by, status FROM decisions ORDER BY id DESC").fetchall()
    if rows:
        st.dataframe([dict(zip(["date", "topic", "decision", "by", "status"], r)) for r in rows],
                     width="stretch")
    else:
        st.caption("No decisions yet. Run a meeting first.")
    mrows = con.execute("SELECT held_on, title, summary FROM meetings ORDER BY id DESC").fetchall()
    for h, t, s in mrows:
        st.markdown(f"**{h} · {t}**: {s}")

# -------- Reminders & digest
with tab_digest:
    today = dt.date.today()
    rows = con.execute("SELECT task, owner, deadline, status FROM tasks").fetchall()
    tasks = [dict(zip(["task", "owner", "deadline", "status"], r)) for r in rows]
    st.subheader("⏰ Reminders: due in the next 48 hours")
    due = [t for t in tasks if t["status"] == "open" and t["deadline"]
           and today <= dt.date.fromisoformat(t["deadline"]) <= today + dt.timedelta(days=2)]
    overdue = [t for t in tasks if t["status"] == "open" and t["deadline"]
               and dt.date.fromisoformat(t["deadline"]) < today]
    for t in overdue:
        st.error(f"OVERDUE · {t['owner']}: {t['task']} (was due {t['deadline']})")
    for t in due:
        st.warning(f"{t['owner']}: {t['task']} is due {t['deadline']}")
    if not (due or overdue):
        st.caption("Nothing due soon.")

    st.subheader("📬 Weekly digest per person")
    owners = sorted({t["owner"] or "Unassigned" for t in tasks})
    week_end = today + dt.timedelta(days=7)
    for o in owners:
        mine = [t for t in tasks if (t["owner"] or "Unassigned") == o]
        with st.expander(f"{o} · {sum(t['status'] == 'open' for t in mine)} open"):
            body = [f"Hi {o}, here's your DecisionLedger week ({today} to {week_end}):"]
            for t in mine:
                mark = "✓" if t["status"] == "done" else "•"
                body.append(f"{mark} {t['task']} (due {t['deadline'] or 'no date'})")
            st.code("\n".join(body), language=None)
