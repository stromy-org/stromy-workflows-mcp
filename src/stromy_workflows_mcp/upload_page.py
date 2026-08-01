"""Browser upload surface for input sessions (ORG-PLAN-164 WS3).

Why a browser page exists at all
--------------------------------
The documents a workflow analyses live on a *person's* machine. An agent in a
Cowork sandbox cannot read them, and routing them through the MCP transport
would mean base64 in a tool argument — which lands the whole document in every
transcript and hits the output-token ceiling besides. So the capability travels
to a browser the person already controls, and the bytes go from that browser
straight to Blob storage. The facade never sees them; it only verifies them
afterwards, by reading back what was actually written.

The authorization model on these routes
---------------------------------------
These three routes are deliberately NOT behind the server's Entra OAuth: the
person holding the link is a client's colleague, not necessarily anyone with an
app role. The capability token *is* the authorization, and it is narrow by
construction — one session, high entropy, stored only as a hash, expiring, and
revoked the moment the session finalizes.

Scope is then derived from the session's OWN row, never from anything the
request supplies. A capability proves "you may act on this session"; the session
record is what says which client that is. Nothing in the request body can widen
that, which is why these handlers build a single-slug scope rather than
accepting one.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import input_sessions, registry
from .blobs import AzureStagedReader, AzureUploadUrlMinter, build_upload_targets
from .scoping import CallerScope
from .uploads import UploadRejected, redact

logger = logging.getLogger(__name__)

# The page holds a live write capability, so it is locked down hard: no framing
# (clickjacking a drop zone is a real attack), no referrer (the token starts in
# the query string), and a CSP with no external origins at all. `connect-src`
# must allow Azure Blob because the PUT goes there directly.
_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "connect-src 'self' https://*.blob.core.windows.net; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    ),
}


def _session_scope(session: input_sessions.InputSession) -> CallerScope:
    """A scope of exactly the session's own client. Never widened by a request."""
    return CallerScope(frozenset({session.client_slug}))


def _token(request: Request, body: dict[str, Any] | None = None) -> str:
    value = (body or {}).get("token") or request.query_params.get("t") or ""
    return str(value)


def _fail(exc: Exception, *, status: int = 400) -> JSONResponse:
    # UploadRejected carries a stable code; anything else is reported as a
    # generic message so an internal error never becomes a client-facing detail.
    if isinstance(exc, UploadRejected):
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=status)
    if isinstance(exc, input_sessions.InputSessionError):
        return JSONResponse({"error": str(exc), "code": "session_invalid"}, status_code=status)
    logger.exception("upload route failed")
    return JSONResponse({"error": "upload failed", "code": "internal"}, status_code=500)


async def _authorize(session_id: str, token: str) -> input_sessions.InputSession:
    import asyncio  # noqa: PLC0415

    def check() -> input_sessions.InputSession:
        with registry.connect() as conn:
            return input_sessions.authorize_capability(conn, session_id, token)

    return await asyncio.to_thread(check)


async def upload_page(request: Request) -> HTMLResponse:
    """Serve the drop-zone page. The token is validated before anything renders."""
    session_id = request.path_params["session_id"]
    try:
        session = await _authorize(session_id, _token(request))
    except Exception as exc:
        # A dead or spent link gets a plain, non-enumerable message.
        return HTMLResponse(
            _MESSAGE_HTML.format(message=_escape(str(exc))),
            status_code=403,
            headers=_SECURITY_HEADERS,
        )
    names = "".join(
        f'<li data-ordinal="{f["ordinal"]}">{_escape(f["display_name"])}'
        f'<span class="state" id="s{f["ordinal"]}">waiting</span></li>'
        for f in session.files
    )
    return HTMLResponse(
        _PAGE_HTML.format(session_id=_escape(session_id), files=names),
        headers=_SECURITY_HEADERS,
    )


async def upload_urls(request: Request) -> JSONResponse:
    """Mint one short-lived write URL per declared file."""
    session_id = request.path_params["session_id"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a bad request
        body = {}
    try:
        session = await _authorize(session_id, _token(request, body))
    except Exception as exc:
        return _fail(exc, status=403)

    import asyncio  # noqa: PLC0415

    def mint() -> list[dict[str, Any]]:
        from .uploads import AcceptedFile  # noqa: PLC0415

        accepted = [
            AcceptedFile(
                display_name=f["display_name"],
                storage_name=f["staging_blob_key"],
                media_type=f["media_type"],
                size_bytes=f["size_bytes"] or 0,
                ordinal=f["ordinal"],
            )
            for f in session.files
        ]
        targets = build_upload_targets(accepted, AzureUploadUrlMinter())
        with registry.connect() as conn:
            input_sessions.mark_uploading(conn, session_id)
        return [
            {
                "ordinal": t.ordinal,
                "name": t.display_name,
                "media_type": t.media_type,
                "url": t.url,
                "expires_at": t.expires_at,
            }
            for t in targets
        ]

    try:
        return JSONResponse({"files": await asyncio.to_thread(mint)}, headers=_SECURITY_HEADERS)
    except Exception as exc:
        # redact() because a partially-built SAS URL can reach an exception
        # message, and a log line outlives the 15-minute token it would contain.
        logger.error("could not mint upload URLs: %s", redact(str(exc)))
        return _fail(exc, status=500)


async def upload_finalize(request: Request) -> JSONResponse:
    """Verify the uploaded bytes and close the session."""
    session_id = request.path_params["session_id"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        session = await _authorize(session_id, _token(request, body))
    except Exception as exc:
        return _fail(exc, status=403)

    import asyncio  # noqa: PLC0415

    def run() -> dict[str, Any]:
        reader = AzureStagedReader()
        with registry.connect() as conn:
            done = input_sessions.finalize(
                conn, session_id, _session_scope(session), fetch_bytes=reader
            )
        return done.public()

    try:
        return JSONResponse(await asyncio.to_thread(run), headers=_SECURITY_HEADERS)
    except Exception as exc:
        return _fail(exc)


def _escape(value: str) -> str:
    import html  # noqa: PLC0415

    return html.escape(str(value), quote=True)


_MESSAGE_HTML = """<!doctype html><meta charset="utf-8">
<title>Upload link</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:32rem;
padding:0 1rem;color:#1c1c1c}}h1{{font-size:1.25rem}}</style>
<h1>This upload link cannot be used</h1><p>{message}</p>
<p>Ask for a new link to be generated.</p>"""


_PAGE_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Upload documents</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:36rem;
padding:0 1rem;color:#1c1c1c}}
h1{{font-size:1.25rem}}
ul{{list-style:none;padding:0}}
li{{display:flex;justify-content:space-between;gap:1rem;padding:.5rem 0;
border-bottom:1px solid #e5e5e5}}
.state{{color:#666;font-size:.875rem}}
.ok{{color:#1a7f37}}.err{{color:#b3261e}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:6px;
background:#1c1c1c;color:#fff;cursor:pointer}}
button[disabled]{{opacity:.5;cursor:default}}
input[type=file]{{margin:1rem 0}}
#done{{margin-top:1rem;font-weight:600}}
</style>
<h1>Upload the documents for this analysis</h1>
<p>Select the files listed below. They upload straight to secure storage — they
are not stored by the chat or the assistant.</p>
<ul id="files">{files}</ul>
<input type="file" id="picker" multiple>
<p><button id="go">Upload and finish</button></p>
<p id="done"></p>
<script>
// The token starts in the query string so the link is a single copyable thing.
// It is read once and then removed from the address bar, so it does not linger
// in browser history or get shoulder-surfed off the URL bar.
const TOKEN = new URLSearchParams(location.search).get("t");
const SESSION = "{session_id}";
history.replaceState(null, "", location.pathname);

const el = (n) => document.getElementById(n);
const setState = (ord, text, cls) => {{
  const s = el("s" + ord);
  s.textContent = text;
  s.className = "state" + (cls ? " " + cls : "");
}};

el("go").addEventListener("click", async () => {{
  const chosen = Array.from(el("picker").files || []);
  if (!chosen.length) {{ el("done").textContent = "Choose the files first."; return; }}
  el("go").disabled = true;
  el("done").textContent = "";
  try {{
    const res = await fetch(`/uploads/${{SESSION}}/urls`, {{
      method: "POST",
      headers: {{ "content-type": "application/json" }},
      body: JSON.stringify({{ token: TOKEN }}),
    }});
    if (!res.ok) throw new Error((await res.json()).error || "could not start upload");
    const {{ files }} = await res.json();

    for (const slot of files) {{
      // Matched by name so the person can pick files in any order; a slot with
      // no matching file is left for the server to reject at finalize, rather
      // than silently uploading the wrong document into it.
      const match = chosen.find((f) => f.name === slot.name);
      if (!match) {{ setState(slot.ordinal, "not selected", "err"); continue; }}
      setState(slot.ordinal, "uploading…");
      const put = await fetch(slot.url, {{
        method: "PUT",
        headers: {{ "x-ms-blob-type": "BlockBlob", "content-type": slot.media_type }},
        body: match,
      }});
      setState(slot.ordinal, put.ok ? "uploaded" : "failed", put.ok ? "ok" : "err");
      if (!put.ok) throw new Error(`upload failed for ${{slot.name}}`);
    }}

    const fin = await fetch(`/uploads/${{SESSION}}/finalize`, {{
      method: "POST",
      headers: {{ "content-type": "application/json" }},
      body: JSON.stringify({{ token: TOKEN }}),
    }});
    const out = await fin.json();
    if (!fin.ok) throw new Error(out.error || "verification failed");
    el("done").textContent = "Done — you can close this page and tell the assistant.";
  }} catch (err) {{
    el("done").textContent = err.message;
    el("go").disabled = false;
  }}
}});
</script>"""
