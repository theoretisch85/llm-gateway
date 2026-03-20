from textwrap import dedent
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import attach_admin_session_cookie, clear_admin_session_cookie, get_admin_session_username, validate_admin_credentials
from app.config import get_settings


router = APIRouter(tags=["admin-auth"])


@router.get("/admin/login", response_class=HTMLResponse, response_model=None)
async def admin_login_page(request: Request, next: str = "/internal/admin") -> HTMLResponse | RedirectResponse:
    if get_admin_session_username(request):
        return RedirectResponse(url=next, status_code=303)
    return HTMLResponse(
        _login_html(next_path=next, error=None),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/admin/login", response_class=HTMLResponse, response_model=None)
async def admin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/internal/admin"),
) -> HTMLResponse | RedirectResponse:
    settings = get_settings()
    if not validate_admin_credentials(username=username, password=password, settings=settings):
        return HTMLResponse(
            _login_html(next_path=next, error="Login fehlgeschlagen."),
            status_code=401,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    response = RedirectResponse(url=next or "/internal/admin", status_code=303)
    attach_admin_session_cookie(response, settings, username)
    return response


@router.post("/admin/logout")
async def admin_logout() -> RedirectResponse:
    response = RedirectResponse(url="/admin/login", status_code=303)
    clear_admin_session_cookie(response)
    return response


def _login_html(next_path: str, error: str | None) -> str:
    safe_next = quote(next_path, safe="/?=&")
    error_block = f'<div class="error">{error}</div>' if error else ""
    return dedent(
        f"""\
        <!doctype html>
        <html lang="de">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>llm-gateway login</title>
          <style>
            :root {{
              --bg:#0b0f14;
              --bg-deep:#06080c;
              --card:#11161d;
              --ink:#d8e4ef;
              --muted:#7f92a3;
              --line:#2b3744;
              --accent:#8be28b;
              --accent-2:#67c1ff;
              --err:#ff8181;
            }}
            * {{ box-sizing:border-box; }}
            body {{
              margin:0;
              min-height:100vh;
              display:grid;
              place-items:center;
              color:var(--ink);
              font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
              background:
                linear-gradient(180deg, #10151c 0%, var(--bg) 48%, var(--bg-deep) 100%);
            }}
            body::before {{
              content:"";
              position:fixed;
              inset:0;
              pointer-events:none;
              background-image:
                linear-gradient(rgba(67,81,96,.10) 1px, transparent 1px),
                linear-gradient(90deg, rgba(67,81,96,.10) 1px, transparent 1px);
              background-size:24px 24px;
            }}
            .card {{
              width:min(480px, calc(100vw - 32px));
              position:relative;
              background:linear-gradient(180deg, #161c24, #11161d);
              border:1px solid var(--line);
              border-radius:10px;
              padding:48px 28px 28px;
            }}
            .card::before {{
              content:"";
              position:absolute;
              top:0;
              left:0;
              right:0;
              height:28px;
              border-bottom:1px solid var(--line);
              border-radius:10px 10px 0 0;
              background:
                radial-gradient(circle at 16px 14px, #ff5f56 0 4px, transparent 5px),
                radial-gradient(circle at 34px 14px, #ffbd2e 0 4px, transparent 5px),
                radial-gradient(circle at 52px 14px, #27c93f 0 4px, transparent 5px),
                linear-gradient(180deg, #1a222b, #161d26);
            }}
            h1 {{
              margin:0 0 8px;
              font-size:1.6rem;
              letter-spacing:.12em;
              text-transform:uppercase;
              color:var(--accent);
            }}
            p {{ margin:0 0 18px; color:var(--muted); line-height:1.45; }}
            label {{ display:block; margin-bottom:14px; }}
            span {{
              display:block;
              margin-bottom:6px;
              font-weight:700;
              font-size:.92rem;
              letter-spacing:.08em;
              text-transform:uppercase;
              color:#a4b5c5;
            }}
            input {{
              width:100%;
              border:1px solid var(--line);
              border-radius:8px;
              padding:12px;
              font:inherit;
              color:var(--ink);
              background:#0a0f14;
            }}
            input:focus {{
              outline:none;
              border-color:#4b8d4b;
              box-shadow:0 0 0 2px rgba(139,226,139,.10);
            }}
            button {{
              width:100%;
              border:1px solid #2e5131;
              border-radius:8px;
              padding:12px 16px;
              background:#1b2a1f;
              color:var(--accent);
              font:inherit;
              font-weight:800;
              cursor:pointer;
              letter-spacing:.06em;
              text-transform:uppercase;
            }}
            .error {{
              margin:0 0 14px;
              padding:10px 12px;
              background:rgba(255,107,107,.12);
              color:var(--err);
              border-radius:10px;
              border:1px solid rgba(255,107,107,.22);
            }}
            .hint {{ margin-top:14px; font-size:.95rem; color:var(--muted); }}
          </style>
        </head>
        <body>
          <form class="card" method="post" action="/admin/login">
            <h1>Admin Login</h1>
            <p>Browser-Zugang fuer Dashboard, Chat, Ops und spaetere Erweiterungen wie Uploads und Pi-Integrationen.</p>
            {error_block}
            <input type="hidden" name="next" value="{safe_next}">
            <label>
              <span>Benutzername</span>
              <input type="text" name="username" autocomplete="username" required>
            </label>
            <label>
              <span>Passwort</span>
              <input type="password" name="password" autocomplete="current-password" required>
            </label>
            <button type="submit">Einloggen</button>
            <div class="hint">Wenn `ADMIN_PASSWORD` nicht gesetzt ist, gilt vorerst dein bestehender `API_BEARER_TOKEN` als Login-Passwort.</div>
          </form>
        </body>
        </html>
        """
    )
