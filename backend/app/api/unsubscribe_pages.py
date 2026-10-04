from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Unsubscribe
from app.services.suppression import SuppressionError, SuppressionService

router = APIRouter(tags=["unsubscribe-pages"])

_PAGE_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f6f8fb;margin:0;padding:0;color:#1f2937}
.wrap{max-width:520px;margin:60px auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:40px;box-shadow:0 4px 16px rgba(0,0,0,.06)}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;font-weight:600}
h1{font-size:22px;margin:8px 0 12px}
p{color:#4b5563;line-height:1.5}
button{margin-top:20px;background:#111827;color:#fff;border:0;border-radius:8px;padding:12px 22px;font-size:15px;cursor:pointer}
button:hover{background:#000}
"""


def _render(title: str, heading: str, body: str, confirm: bool = False, action: str | None = None) -> str:
    button = ""
    if confirm and action:
        button = f'<form method="post" action="{action}"><button type="submit">Confirm unsubscribe</button></form>'
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><style>{_PAGE_CSS}</style></head><body><main class="wrap"><p class="eyebrow">Email Preferences</p><h1>{heading}</h1><p>{body}</p>{button}</main></body></html>"""


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str, session: Session = Depends(get_db)) -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    record = session.scalar(select(Unsubscribe).where(Unsubscribe.token_hash == token_hash))
    if record is None:
        raise HTTPException(status_code=404, detail="Unsubscribe link is invalid or has expired")
    return _render(
        "Confirm unsubscribe",
        "Unsubscribe from these emails?",
        "You are about to remove this email address from future campaign communications. This can only be undone by an account administrator.",
        confirm=True,
        action=f"/unsubscribe/{token}/confirm",
    )


@router.post("/unsubscribe/{token}/confirm", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, session: Session = Depends(get_db)) -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    record = session.scalar(select(Unsubscribe).where(Unsubscribe.token_hash == token_hash))
    if record is None:
        raise HTTPException(status_code=404, detail="Unsubscribe link is invalid or has expired")
    try:
        SuppressionService(session, record.tenant_id).consume_unsubscribe_token(token)
    except SuppressionError:
        raise HTTPException(status_code=404, detail="Unsubscribe link is invalid or has expired") from None
    return _render(
        "You are unsubscribed",
        "You\u2019re unsubscribed",
        "Your email address has been removed from future campaign communications. If you change your mind, contact the sender to be added back.",
    )
