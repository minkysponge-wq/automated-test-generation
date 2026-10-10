"""
Gemini client + background task pool.

* Connection: with GEMINI_USE_VERTEX=auto the AI Studio endpoint is tried first, then Vertex AI
  (express-mode keys). The working mode is cached.
* Models: GEMINI_MODEL_FAST / GEMINI_MODEL_PRO from .env, or - when empty or invalid - the newest
  `gemini-X.Y-flash` and `gemini-X.Y-pro` returned by models.list().
* `extract_json()` = structured output (response_schema from Pydantic), temperature 0, 90 s timeout,
  2 retries with backoff on 429/5xx. Every call is logged in `ai_extractions` (model, prompt version,
  latency, tokens, raw response).
* Calls never run inside an HTTP request: `submit()` queues them on a small thread pool and the
  browser polls /api/ai/tasks/<id>.
* The API key is never printed or logged (diagnostics show the last 4 characters only).
"""
import json
import logging
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import config
from .. import audit, db
from ..db import now_ist

log = logging.getLogger("atrg.ai")
_pool = None
_lock = threading.Lock()
_state = {"client": None, "mode": None, "fast": None, "pro": None, "error": None, "checked": 0.0}


class AIError(Exception):
    """Short, user-facing reason (the full traceback goes to the log and the task row)."""


def masked_key():
    k = config.GEMINI_API_KEY
    return f"...{k[-4:]}" if k else "(none)"


def available():
    return bool(config.AI_ENABLED and config.GEMINI_API_KEY)


def disabled_reason():
    if not config.GEMINI_API_KEY:
        return "Automatic reading is not configured (no GEMINI_API_KEY in .env)"
    if not config.AI_ENABLED:
        return "Automatic reading is turned off by the administrator"
    return None


def _newest(names, kind):
    pat = re.compile(rf"^gemini-(\d+(?:\.\d+)?)-{kind}(-preview)?$")
    found = [(float(m.group(1)), not n.endswith("preview"), n) for n in names for m in [pat.match(n)] if m]
    return max(found)[2] if found else None


def connect(force=False):
    """Create (and cache) the client, choose the models. Returns the state dict."""
    with _lock:
        if _state["client"] is not None and not force:
            return _state
        if not available():
            _state.update(client=None, error=disabled_reason())
            return _state
        from google import genai
        modes = {"auto": ["aistudio", "vertex"], "true": ["vertex"], "false": ["aistudio"]}.get(
            config.GEMINI_USE_VERTEX, ["aistudio", "vertex"])
        errors = []
        for mode in modes:
            try:
                c = (genai.Client(api_key=config.GEMINI_API_KEY) if mode == "aistudio"
                     else genai.Client(vertexai=True, api_key=config.GEMINI_API_KEY))
                names = [m.name.split("/")[-1] for m in c.models.list()]
            except Exception as exc:
                errors.append(f"{mode}: {_short(exc)}")
                continue
            fast = config.GEMINI_MODEL_FAST if config.GEMINI_MODEL_FAST in names else _newest(names, "flash")
            pro = config.GEMINI_MODEL_PRO if config.GEMINI_MODEL_PRO in names else (_newest(names, "pro") or fast)
            if not fast:
                errors.append(f"{mode}: no Gemini Flash model available to this key")
                continue
            _state.update(client=c, mode=mode, fast=fast, pro=pro, error=None, checked=time.time())
            return _state
        _state.update(client=None, error="; ".join(errors) or "unknown error", checked=time.time())
        return _state


def status_line():
    s = connect()
    if s["client"] is None:
        return f"Gemini: FAILED - {s['error']}"
    return f"Gemini: OK (mode={s['mode']}, fast={s['fast']}, pro={s['pro']})"


def status_text():  # kept for older callers
    return status_line()


def check_key(force=False):
    s = connect(force)
    return {"ok": s["client"] is not None if available() else None, "message": status_line() + f" key {masked_key()}"}


def _short(exc):
    msg = str(exc)
    low = msg.lower()
    if "429" in msg or "resource_exhausted" in low or "quota" in low:
        return "quota exceeded, try again in a minute"
    if "401" in msg or "403" in msg or "permission" in low or "api key" in low:
        return "the API key was rejected"
    if "timeout" in low or "deadline" in low:
        return "the AI service did not answer in time"
    if "not found" in low and "model" in low:
        return "the configured model is not available"
    return msg.splitlines()[0][:160]


def extract_json(parts, schema, system_prompt, model="fast", prompt_version="", conn=None, task_id=None, kind="",
                 file_id=None):
    """
    One structured-output call. parts = list of str | (bytes, mime). model = 'fast' | 'pro' | model id.
    Returns (data_dict, meta{model, latency_ms, tokens}). Raises AIError with a short reason.
    """
    from google.genai import types
    s = connect()
    if s["client"] is None:
        raise AIError(s["error"] or "AI unavailable")
    model_id = s.get(model) or model
    contents = [types.Part.from_bytes(data=p[0], mime_type=p[1]) if isinstance(p, tuple) else p for p in parts]
    extra = {}
    if model == "fast":     # transcription needs little reasoning: low thinking keeps large forms well under the timeout
        extra["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    cfg = types.GenerateContentConfig(system_instruction=system_prompt, temperature=0,
                                      response_mime_type="application/json", response_schema=schema,
                                      http_options=types.HttpOptions(timeout=90_000), **extra)
    last = None
    for attempt in range(3):
        t0 = time.time()
        try:
            resp = s["client"].models.generate_content(model=model_id, contents=contents, config=cfg)
            latency = int((time.time() - t0) * 1000)
            raw = resp.text or ""
            tokens = getattr(resp.usage_metadata, "total_token_count", None) if resp.usage_metadata else None
            if conn is not None:
                record(conn, task_id, kind, file_id, model_id, prompt_version, latency, raw, tokens)
            return (json.loads(raw) if raw else {}), {"model": model_id, "latency_ms": latency, "tokens": tokens}
        except Exception as exc:
            last = exc
            msg = str(exc)
            log.error("Gemini call failed (attempt %s, model %s):\n%s", attempt + 1, model_id, traceback.format_exc())
            if conn is not None:
                record(conn, task_id, kind, file_id, model_id, prompt_version, int((time.time() - t0) * 1000),
                       f"ERROR: {msg[:2000]}", None)
            retryable = (any(c in msg for c in ("429", "500", "502", "503", "504")) or "RESOURCE_EXHAUSTED" in msg
                         or "timed out" in msg.lower() or "deadline" in msg.lower())
            if not retryable or attempt == 2:
                break
            time.sleep(2 * (attempt + 1) ** 2)
    raise AIError(f"AI reading failed: {_short(last)}")


def record(conn, task_id, kind, file_id, model, prompt_version, latency_ms, raw, tokens=None):
    cur = conn.execute("INSERT INTO ai_extractions(task_id, kind, file_id, model, prompt_version, latency_ms, tokens, "
                       "raw_response, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (task_id, kind, file_id, model, prompt_version, latency_ms, tokens, raw, now_ist()))
    return cur.lastrowid


# =============================================================================
# background tasks
# =============================================================================
def _executor():
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=config.AI_WORKERS, thread_name_prefix="atrg-ai")
        return _pool


def submit(conn, kind, input_data, fn, user):
    """Queue fn(task_id, conn) -> result dict. Returns the task id immediately."""
    cur = conn.execute("INSERT INTO ai_tasks(kind, status, input_json, created_by, created_at) VALUES (?,?,?,?,?)",
                       (kind, "queued", json.dumps(input_data, default=str), user["id"] if user else None, now_ist()))
    task_id = cur.lastrowid
    if conn.in_transaction:
        conn.execute("COMMIT")
        conn.execute("BEGIN IMMEDIATE")
    _executor().submit(_run, task_id, fn, user)
    return task_id


def progress(conn, task_id, text):
    """Plain-text progress shown while the task runs ("Reading document - page 2 of 3")."""
    if task_id:
        conn.execute("UPDATE ai_tasks SET error=? WHERE id=? AND status='running'", (f"progress:{text}", task_id))


def _run(task_id, fn, user):
    conn = db.connect()
    try:
        conn.execute("UPDATE ai_tasks SET status='running', started_at=? WHERE id=?", (now_ist(), task_id))
        result = fn(task_id, conn)
        conn.execute("UPDATE ai_tasks SET status='done', error=NULL, result_json=?, finished_at=? WHERE id=?",
                     (json.dumps(result, default=str), now_ist(), task_id))
        audit.log(conn, "ai.task_done", "ai_task", task_id, None,
                  {"kind": result.get("kind"), "summary": result.get("summary")}, user=user)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, AIError) else f"Reading failed: {_short(exc)}"
        log.error("AI task %s failed:\n%s", task_id, traceback.format_exc())
        conn.execute("UPDATE ai_tasks SET status='failed', error=?, result_json=?, finished_at=? WHERE id=?",
                     (reason, json.dumps({"traceback": traceback.format_exc()[-4000:]}), now_ist(), task_id))
        audit.log(conn, "ai.task_failed", "ai_task", task_id, None, {"error": reason}, user=user)
    finally:
        conn.close()


def task(conn, task_id):
    row = conn.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    if out["status"] == "failed" and out["result"]:
        out["result"] = None                      # tracebacks stay server-side
    out["progress"] = row["error"][9:] if (row["error"] or "").startswith("progress:") else None
    if out["progress"]:
        out["error"] = None
    return out
