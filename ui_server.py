"""CostPilot web UI backend.

Serves a single-page UI and a tiny JSON API on localhost only. Runs call the same
analyze_to_files() as the CLI, so the UI adds no new AWS or model access.

    python ui_server.py            # then open http://127.0.0.1:8000
"""
import asyncio
import html
import os
import re
import time
import uuid
import webbrowser

import botocore.session
import markdown
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import analyze_to_files
from copilot import ChatSession
from render import normalize_markdown

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
HOST = "127.0.0.1"  # localhost only: the UI can start analyses against your AWS profiles
PORT = int(os.environ.get("COSTPILOT_PORT", "8000"))

os.makedirs(REPORTS, exist_ok=True)
app = FastAPI(title="CostPilot")
app.mount("/reports", StaticFiles(directory=REPORTS), name="reports")

RUNS: dict[str, dict] = {}


class RunRequest(BaseModel):
    profiles: list[str]
    regions: str = "auto"


def available_profiles() -> list[str]:
    return sorted(botocore.session.Session().available_profiles)


async def execute(run_id: str, req: RunRequest):
    run = RUNS[run_id]

    def log(msg: str):
        run["log"].append(msg)

    for profile in req.profiles:  # one account at a time
        try:
            account, base = await analyze_to_files(profile, req.regions, None, REPORTS, log)
            run["results"].append({"profile": profile, "account": account,
                                   "file": os.path.basename(base) + ".html"})
        except Exception as e:  # noqa: BLE001 - one bad profile must not stop the others
            log(f"[{profile}] FAILED: {e}")
            run["results"].append({"profile": profile, "error": str(e)})
    run["status"] = "done"
    run["finished"] = time.time()


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "ui", "index.html"))


@app.get("/api/profiles")
def profiles():
    return {"profiles": available_profiles()}


@app.post("/api/runs")
async def start_run(req: RunRequest):
    if any(r["status"] == "running" for r in RUNS.values()):
        raise HTTPException(409, "An analysis is already running")
    known = set(available_profiles())
    bad = [p for p in req.profiles if p not in known]
    if not req.profiles or bad:
        raise HTTPException(400, f"Unknown or missing profile(s): {bad or 'none selected'}")
    check_regions(req.regions)
    run_id = uuid.uuid4().hex[:8]
    RUNS[run_id] = {"status": "running", "log": [], "results": [], "started": time.time()}
    asyncio.create_task(execute(run_id, req))
    return {"id": run_id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, since: int = 0):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    return {"status": run["status"], "log": run["log"][since:], "next": len(run["log"]),
            "results": run["results"], "elapsed": round((run.get("finished") or time.time()) - run["started"])}


MAX_CHATS = 3
MAX_MESSAGE_CHARS = 2000
CHATS: dict[str, ChatSession] = {}


class ChatStart(BaseModel):
    profile: str
    regions: str = "auto"


class ChatMessage(BaseModel):
    text: str


def check_regions(regions: str):
    if regions != "auto" and not re.fullmatch(r"[a-z0-9-]+(,\s*[a-z0-9-]+)*", regions):
        raise HTTPException(400, "regions must be 'auto' or a comma-separated list like us-east-1,us-east-2")


def render_event(ev: dict) -> dict:
    """Assistant text is model output built from AWS data: escape it before Markdown so it can never inject HTML."""
    if ev["type"] == "text":
        text = html.escape(normalize_markdown(ev["text"]))
        return {**ev, "html": markdown.markdown(text, extensions=["tables", "sane_lists", "fenced_code", "nl2br"])}
    return ev


@app.post("/api/chats")
async def start_chat(req: ChatStart):
    for cid in [c for c, s in CHATS.items() if s.status == "closed"]:
        del CHATS[cid]
    if len(CHATS) >= MAX_CHATS:
        raise HTTPException(409, f"At most {MAX_CHATS} chats at a time; close one first")
    if req.profile not in set(available_profiles()):
        raise HTTPException(400, f"Unknown profile: {req.profile}")
    check_regions(req.regions)
    chat_id = uuid.uuid4().hex[:8]
    CHATS[chat_id] = ChatSession(chat_id, req.profile, req.regions, REPORTS)
    return {"id": chat_id}


@app.post("/api/chats/{chat_id}/messages")
def send_message(chat_id: str, msg: ChatMessage):
    chat = CHATS.get(chat_id)
    if not chat or chat.status == "closed":
        raise HTTPException(404, "Unknown or closed chat")
    if chat.status != "idle":
        raise HTTPException(409, "Copilot is still working on the previous message")
    text = msg.text.strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Message must be 1-{MAX_MESSAGE_CHARS} characters")
    chat.send(text)
    return {"ok": True}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, since: int = 0):
    chat = CHATS.get(chat_id)
    if not chat:
        raise HTTPException(404, "Unknown chat")
    return {"status": chat.status, "events": [render_event(e) for e in chat.events[since:]],
            "next": len(chat.events)}


@app.delete("/api/chats/{chat_id}")
def close_chat(chat_id: str):
    chat = CHATS.get(chat_id)
    if chat:
        chat.close()
    return {"ok": True}


@app.get("/api/reports")
def reports():
    out = []
    for name in os.listdir(REPORTS):
        m = re.fullmatch(r"costpilot-(\d+)-(\d{8})\.html", name)
        if m:
            out.append({"file": name, "account": m.group(1), "date": m.group(2),
                        "modified": os.path.getmtime(os.path.join(REPORTS, name))})
    return {"reports": sorted(out, key=lambda r: -r["modified"])}


if __name__ == "__main__":
    print(f"CostPilot UI: http://{HOST}:{PORT}  (Ctrl+C to stop)")
    webbrowser.open(f"http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
