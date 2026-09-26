#!/usr/bin/env python3
"""
Telegram → SolidInvoice Cloud bot (same command surface as the Invoice Ninja bot).

Env (Coolify secrets)
---------------------
    TG_TOKEN              Telegram bot token from @BotFather
    SI_URL                Cloud base, no trailing slash
                          e.g. https://xxxx.solidinvoice.app
    SI_TOKEN              Settings → API Keys → Create Token (X-API-TOKEN)
    SI_CURRENCY           ISO code used when creating clients (default JMD)
    SI_DECIMALS           Minor units for prices (default 2 → 4275.00 becomes 427500)
    ALLOWED_USER_IDS      Optional comma-separated Telegram user ids. Empty = any user.

    pip install "python-telegram-bot>=21" requests
    python telegram_solidinvoice_bot.py

SolidInvoice notes
------------------
* Money on the API is integer minor units (cents). You still type 4275 in chat.
* Documents use a human invoiceId/quoteId AND an internal ULID.
* Quotes convert with POST /api/quotes/{ulid}/invoice
* Paid is POST /api/invoices/{ulid}/transitions/pay (and a payment if needed).
* Cloud rate limit: 300 req/min per token.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("si-bot")

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
SI_URL = os.environ.get("SI_URL", "").strip().rstrip("/")
SI_TOKEN = os.environ.get("SI_TOKEN", "").strip()
SI_CURRENCY = os.environ.get("SI_CURRENCY", "JMD").strip().upper() or "JMD"
SI_DECIMALS = int(os.environ.get("SI_DECIMALS", "2"))
ALLOWED_RAW = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {int(x) for x in ALLOWED_RAW.split(",") if x.strip().isdigit()}

TIMEOUT = 90

# chat_id → draft state
DRAFTS: dict[int, dict[str, Any]] = {}

HELP_TEXT = """\
*SolidInvoice bot*

Talk here. SolidInvoice Cloud keeps the books. Type *YES* to save a draft.

*Setup*
/start — this help
/help — this help
/whoami — your Telegram id (put it in ALLOWED\\_USER\\_IDS)

*Clients*
/client Name, email, phone
/client Jane Doe, jane@shop.com, 8765550000
/clients — recent clients
/use Jane — select the client for the next quote/invoice

*Items* (after /quote or /invoice)
One line or many lines. Commas separate fields. No comma inside a description.

`name, description, unit price, qty`

/item
Oil filter, car oil filter, 4275, 5
Cabin air filter, premium filter, 3365, 2

Same thing one line:
/item Oil filter, car oil filter, 4275, 5, Cabin air filter, premium filter, 3365, 2

*Documents*
/quote — start a quote for the selected client, then /item, then YES
/invoice — start an invoice the same way
/quote 104 — show quote 104
/invoice 88 — show invoice 88
/convert 104 — quote 104 → invoice

*Money*
/unpaid — invoices not paid
/unpaid Jane — unpaid for that client name
/paid 88 — mark invoice 88 paid

Prices you type are *normal money* (4275 = 4275.00). The API stores minor units.
"""


def require_env() -> None:
    missing = [n for n, v in (("TG_TOKEN", TG_TOKEN), ("SI_URL", SI_URL), ("SI_TOKEN", SI_TOKEN)) if not v]
    if missing:
        raise SystemExit("Missing env: " + ", ".join(missing))


def allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not ALLOWED_USER_IDS:
        return True
    return user.id in ALLOWED_USER_IDS


def headers() -> dict[str, str]:
    return {
        "X-API-TOKEN": SI_TOKEN,
        "Accept": "application/ld+json",
        "Content-Type": "application/ld+json",
    }


def to_minor(amount: float | int | str) -> int:
    scale = 10**SI_DECIMALS
    return int(round(float(amount) * scale))


def from_minor(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        raw = raw.get("amount", raw.get("value", 0))
    scale = 10**SI_DECIMALS
    try:
        return float(raw) / scale
    except (TypeError, ValueError):
        return 0.0


def iri_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("@id") or value.get("id") or ""
    text = str(value)
    return text.rstrip("/").split("/")[-1]


def hydra_members(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("hydra:member", "member"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def si_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    url = path if path.startswith("http") else f"{SI_URL}{path}"
    kwargs.setdefault("timeout", TIMEOUT)
    hdrs = headers()
    extra = kwargs.pop("headers", {})
    hdrs.update(extra)
    resp = requests.request(method, url, headers=hdrs, **kwargs)
    if resp.status_code == 429:
        raise RuntimeError("SolidInvoice rate limit (300/min). Wait and retry.")
    return resp


def si_json(method: str, path: str, **kwargs: Any) -> Any:
    resp = si_request(method, path, **kwargs)
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:500]
        raise RuntimeError(f"SI {resp.status_code} {path}: {snippet}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}


def parse_item_chunks(blob: str) -> list[dict[str, Any]]:
    """
    Groups of 4: name, description, price, qty.
    Newlines or a long comma list.
    """
    lines: list[str] = []
    for raw in blob.replace("\r", "").split("\n"):
        raw = raw.strip()
        if raw:
            lines.append(raw)
    if not lines:
        return []

    # If every line already has 4 fields, parse per line.
    per_line: list[dict[str, Any]] = []
    all_four = True
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            name, desc, price, qty = parts
            per_line.append(
                {
                    "name": name,
                    "description": desc or name,
                    "price": float(price),
                    "qty": float(qty),
                }
            )
        else:
            all_four = False
            break
    if all_four:
        return per_line

    # Flatten commas into groups of 4.
    parts = [p.strip() for p in ",".join(lines).split(",") if p.strip()]
    if len(parts) % 4 != 0:
        raise ValueError(
            "Items must be groups of 4: name, description, price, qty. "
            "Do not put commas inside the description."
        )
    out: list[dict[str, Any]] = []
    for i in range(0, len(parts), 4):
        name, desc, price, qty = parts[i : i + 4]
        out.append(
            {
                "name": name,
                "description": desc or name,
                "price": float(price),
                "qty": float(qty),
            }
        )
    return out


def split_person_name(full: str) -> tuple[str, str]:
    bits = full.strip().split()
    if not bits:
        return "Client", ""
    if len(bits) == 1:
        return bits[0], ""
    return bits[0], " ".join(bits[1:])


def format_money(amount_major: float) -> str:
    return f"{SI_CURRENCY} {amount_major:,.{SI_DECIMALS}f}"


def draft_summary(draft: dict[str, Any]) -> str:
    client = draft.get("client") or {}
    kind = draft.get("kind", "invoice")
    rows = draft.get("items") or []
    lines = [
        f"*{kind.title()} draft*",
        f"Client: {client.get('name', '?')}",
        "",
    ]
    total = 0.0
    for row in rows:
        line_total = row["price"] * row["qty"]
        total += line_total
        lines.append(
            f"• {row['name']} × {row['qty']} @ {format_money(row['price'])} = {format_money(line_total)}"
        )
    lines += ["", f"*Total* {format_money(total)}", "", "Reply *YES* to save, /cancel to drop."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SolidInvoice helpers
# ---------------------------------------------------------------------------


def list_clients(query: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"itemsPerPage": 50}
    if query:
        params["name"] = query
    data = si_json("GET", "/api/clients", params=params)
    members = hydra_members(data)
    if query:
        q = query.lower()
        members = [c for c in members if q in str(c.get("name", "")).lower()]
    return members


def contact_iri(client: dict[str, Any], contact: dict[str, Any]) -> str:
    """SolidInvoice wants /api/clients/{client}/contact/{id} on quote.users."""
    existing = contact.get("@id")
    if isinstance(existing, str) and "/contact" in existing:
        return existing
    cid = iri_id(client.get("@id") or client.get("id"))
    kid = iri_id(contact.get("@id") or contact.get("id"))
    if cid and kid:
        return f"/api/clients/{cid}/contact/{kid}"
    if existing:
        return str(existing)
    return ""


def get_client_contacts(client: dict[str, Any]) -> list[dict[str, Any]]:
    cid = iri_id(client.get("@id") or client.get("id"))
    collected: list[dict[str, Any]] = []
    if cid:
        for path, params in (
            (f"/api/clients/{cid}/contacts", None),
            ("/api/contacts", {"client": f"/api/clients/{cid}", "itemsPerPage": 50}),
        ):
            try:
                data = si_json("GET", path, params=params)
            except RuntimeError:
                continue
            collected.extend(hydra_members(data))
            if collected:
                break
    for key in ("contacts", "users"):
        embedded = client.get(key) or []
        if isinstance(embedded, list):
            collected.extend(x for x in embedded if isinstance(x, dict))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in collected:
        key = iri_id(row)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


def ensure_client_contact(client: dict[str, Any]) -> list[str]:
    """Return quote/invoice `users` IRIs. Create a contact if the client has none."""
    contacts = get_client_contacts(client)
    if not contacts:
        cid = iri_id(client.get("@id") or client.get("id"))
        first, last = split_person_name(str(client.get("name") or "Client"))
        email = (
            client.get("email")
            or (client.get("_contact") or {}).get("email")
            or f"noreply+{cid or 'client'}@placeholder.local"
        )
        body = {"firstName": first, "lastName": last or first, "email": email}
        created = None
        for path in (f"/api/clients/{cid}/contacts", "/api/contacts"):
            payload = dict(body)
            if path == "/api/contacts":
                payload["client"] = client.get("@id") or f"/api/clients/{cid}"
            try:
                created = si_json("POST", path, json=payload)
                break
            except RuntimeError as exc:
                log.warning("Contact create %s failed: %s", path, exc)
        if created:
            contacts = [created]
            client["_contact"] = created
    iris = [contact_iri(client, c) for c in contacts]
    return [u for u in iris if u]


def create_client(name: str, email: str, phone: str) -> dict[str, Any]:
    first, last = split_person_name(name)
    payload: dict[str, Any] = {
        "name": name.strip(),
        "currency": SI_CURRENCY,
    }
    created = si_json("POST", "/api/clients", json=payload)
    cid = iri_id(created.get("@id") or created.get("id"))
    created["email"] = email.strip()
    contact_body = {
        "firstName": first,
        "lastName": last or first,
        "email": email.strip(),
    }
    if phone.strip():
        contact_body["phone"] = phone.strip()
    contact = None
    last_err = None
    for path, extra in (
        (f"/api/clients/{cid}/contacts", {}),
        ("/api/contacts", {"client": created.get("@id") or f"/api/clients/{cid}"}),
    ):
        try:
            payload_c = dict(contact_body)
            payload_c.update(extra)
            contact = si_json("POST", path, json=payload_c)
            break
        except RuntimeError as exc:
            last_err = exc
            log.warning("Contact create %s failed: %s", path, exc)
    if contact:
        created["_contact"] = contact
    elif last_err:
        created["_contact_error"] = str(last_err)
    return created


def find_document(kind: str, number: str) -> dict[str, Any] | None:
    """kind is invoices or quotes. number is human invoiceId/quoteId or ULID."""
    path = "/api/invoices" if kind == "invoices" else "/api/quotes"
    id_field = "invoiceId" if kind == "invoices" else "quoteId"
    data = si_json("GET", path, params={"itemsPerPage": 100})
    number = number.strip()
    for row in hydra_members(data):
        if str(row.get(id_field, "")).strip() == number:
            return row
        if iri_id(row) == number:
            return row
    # Direct ULID get
    try:
        return si_json("GET", f"{path}/{number}")
    except RuntimeError:
        return None


def line_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in items:
        desc = row.get("description") or row["name"]
        if row["name"] and row["name"] not in desc:
            desc = f"{row['name']} — {desc}"
        out.append(
            {
                "description": desc,
                "qty": row["qty"],
                "price": to_minor(row["price"]),
            }
        )
    return out


def create_document(kind: str, client: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    cid = client.get("@id") or f"/api/clients/{iri_id(client)}"
    users = ensure_client_contact(client)
    if not users:
        raise RuntimeError(
            "This client has no contact. SolidInvoice will not save a quote "
            "without one. Use /client Name, email, phone so a contact is created."
        )
    body: dict[str, Any] = {
        "client": cid,
        "lines": line_payloads(items),
        "users": users,
    }
    path = "/api/quotes" if kind == "quote" else "/api/invoices"
    try:
        doc = si_json("POST", path, json=body)
    except RuntimeError as exc:
        # Some Cloud builds want /api/contacts/{id} instead of nested contact IRI.
        if "users" in str(exc).lower():
            alt = []
            for u in users:
                kid = iri_id(u)
                alt.append(f"/api/contacts/{kid}")
            body["users"] = alt
            doc = si_json("POST", path, json=body)
        else:
            raise
    # Move draft → pending/sent when a transition exists.
    uid = iri_id(doc)
    for transition in ("accept", "send", "pending"):
        try:
            doc = si_json("POST", f"{path}/{uid}/transitions/{transition}", json={})
            break
        except RuntimeError:
            continue
    return doc


def convert_quote(quote: dict[str, Any]) -> dict[str, Any]:
    uid = iri_id(quote)
    return si_json("POST", f"/api/quotes/{uid}/invoice", json={})


def mark_paid(invoice: dict[str, Any]) -> dict[str, Any]:
    uid = iri_id(invoice)
    last_err = None
    for transition in ("pay", "paid"):
        try:
            return si_json("POST", f"/api/invoices/{uid}/transitions/{transition}", json={})
        except RuntimeError as exc:
            last_err = exc
    # Fallback: record a payment for the balance.
    amount = invoice.get("balance", invoice.get("total", invoice.get("payableAmount", 0)))
    minor = amount if isinstance(amount, int) else to_minor(from_minor(amount))
    currency = SI_CURRENCY
    client = invoice.get("client")
    body = {
        "invoice": invoice.get("@id") or f"/api/invoices/{uid}",
        "amount": minor,
        "currency": currency,
        "status": "captured",
    }
    if isinstance(client, str):
        body["client"] = client
    elif isinstance(client, dict) and client.get("@id"):
        body["client"] = client["@id"]
    try:
        si_json("POST", "/api/payments", json=body)
        return si_json("GET", f"/api/invoices/{uid}")
    except RuntimeError as exc:
        raise RuntimeError(f"Could not mark paid ({last_err}); payment also failed: {exc}") from exc


def unpaid_invoices(client_name: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for status in ("pending", "overdue", "draft", "sent", "viewed"):
        try:
            data = si_json(
                "GET",
                "/api/invoices",
                params={"status": status, "itemsPerPage": 50},
            )
            out.extend(hydra_members(data))
        except RuntimeError:
            continue
    # De-dupe
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in out:
        key = iri_id(row)
        if key in seen:
            continue
        if str(row.get("status", "")).lower() == "paid":
            continue
        seen.add(key)
        unique.append(row)
    if client_name:
        needle = client_name.lower()
        filtered = []
        for row in unique:
            c = row.get("client")
            name = ""
            if isinstance(c, dict):
                name = str(c.get("name", ""))
            elif isinstance(c, str):
                try:
                    det = si_json("GET", c if c.startswith("/") else f"/api/clients/{iri_id(c)}")
                    name = str(det.get("name", ""))
                    row["_client_name"] = name
                except RuntimeError:
                    name = c
            if needle in name.lower():
                filtered.append(row)
        return filtered
    return unique


def try_pdf(kind: str, doc: dict[str, Any]) -> bytes | None:
    uid = iri_id(doc)
    uuid = doc.get("uuid") or ""
    number = doc.get("invoiceId") if kind == "invoices" else doc.get("quoteId")
    candidates = [
        f"/api/{kind}/{uid}/pdf",
        f"/api/{kind}/{uid}?_format=pdf",
        f"/{kind}/{uid}/pdf",
        f"/invoices/pdf/{uid}" if kind == "invoices" else f"/quotes/pdf/{uid}",
    ]
    if uuid:
        candidates.append(f"/api/{kind}/{uuid}/pdf")
    if number:
        candidates.append(f"/api/{kind}/{number}/pdf")
    for path in candidates:
        try:
            resp = si_request(
                "GET",
                path,
                headers={"Accept": "application/pdf"},
            )
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and (
                "pdf" in ctype.lower() or (resp.content[:5] == b"%PDF-")
            ):
                return resp.content
        except Exception:
            continue
    return None


def describe_doc(kind: str, doc: dict[str, Any]) -> str:
    number = doc.get("invoiceId") or doc.get("quoteId") or iri_id(doc)
    status = doc.get("status", "?")
    total = from_minor(doc.get("total") or doc.get("payableAmount") or 0)
    client = doc.get("client")
    cname = client.get("name") if isinstance(client, dict) else str(client or "")
    return f"{kind[:-1].title()} *{number}* — {status} — {format_money(total)}\nClient: {cname}"


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


async def guard(update: Update) -> bool:
    if not update.effective_user or not update.message:
        return False
    if not allowed(update):
        await update.message.reply_text("This bot is locked to specific Telegram users.")
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_markdown(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    await update.message.reply_text(f"Your Telegram id: {update.effective_user.id}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    DRAFTS.pop(update.effective_chat.id, None)
    await update.message.reply_text("Draft cleared.")


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        rows = list_clients()
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not rows:
        await update.message.reply_text("No clients yet. /client Name, email, phone")
        return
    lines = []
    for c in rows[:20]:
        lines.append(f"• {c.get('name')} ({iri_id(c)[:8]}…)")
    await update.message.reply_text("\n".join(lines))


async def cmd_client(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    raw = " ".join(context.args or []).strip()
    if not raw and update.message and update.message.text:
        raw = update.message.text.partition(" ")[2].strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 2:
        await update.message.reply_text("Usage: /client Name, email, phone")
        return
    name, email = parts[0], parts[1]
    phone = parts[2] if len(parts) > 2 else ""
    if "@" not in email:
        await update.message.reply_text("Second field must be an email.")
        return
    try:
        created = create_client(name, email, phone)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    chat_id = update.effective_chat.id
    draft = DRAFTS.get(chat_id) or {}
    draft["client"] = created
    DRAFTS[chat_id] = draft
    extra = ""
    if created.get("_contact_error"):
        extra = "\n(Client saved; contact/email may need a fix in SolidInvoice.)"
    await update.message.reply_text(f"Saved client {created.get('name')}.{extra}\nSelected for the next quote/invoice.")


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.message.reply_text("Usage: /use Jane")
        return
    try:
        rows = list_clients(q)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not rows:
        await update.message.reply_text("No matching client.")
        return
    if len(rows) > 1:
        names = ", ".join(r.get("name", "?") for r in rows[:8])
        await update.message.reply_text(f"Several matches: {names}. Be more specific.")
        return
    chat_id = update.effective_chat.id
    draft = DRAFTS.get(chat_id) or {}
    draft["client"] = rows[0]
    DRAFTS[chat_id] = draft
    await update.message.reply_text(f"Using {rows[0].get('name')}.")


def start_kind(kind: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        arg = " ".join(context.args or []).strip()
        if arg:
            await show_document(update, "quotes" if kind == "quote" else "invoices", arg)
            return
        chat_id = update.effective_chat.id
        draft = DRAFTS.get(chat_id) or {}
        if not draft.get("client"):
            await update.message.reply_text("Select a client first: /use Name  or  /client Name, email, phone")
            return
        draft["kind"] = kind
        draft["items"] = []
        DRAFTS[chat_id] = draft
        await update.message.reply_text(
            f"{kind.title()} started for {draft['client'].get('name')}. Add /item lines, then YES."
        )

    return handler


async def show_document(update: Update, kind: str, number: str) -> None:
    try:
        doc = find_document(kind, number)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not doc:
        await update.message.reply_text(f"No {kind[:-1]} {number}.")
        return
    await update.message.reply_markdown(describe_doc(kind, doc))
    pdf = try_pdf(kind, doc)
    if pdf:
        number_label = doc.get("invoiceId") or doc.get("quoteId") or number
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf)
            path = tmp.name
        try:
            with open(path, "rb") as fh:
                await update.message.reply_document(document=fh, filename=f"{number_label}.pdf")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    else:
        await update.message.reply_text(
            "No PDF endpoint on this Cloud plan/version. Open the document in SolidInvoice to download."
        )


async def cmd_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    draft = DRAFTS.get(chat_id)
    if not draft or not draft.get("kind"):
        await update.message.reply_text("Start with /quote or /invoice first.")
        return
    text = update.message.text or ""
    blob = text.partition(" ")[2].strip()
    if not blob:
        await update.message.reply_text("Send items on the same message or the next lines after /item.")
        draft["awaiting_items"] = True
        DRAFTS[chat_id] = draft
        return
    try:
        rows = parse_item_chunks(blob)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    draft.setdefault("items", []).extend(rows)
    draft["awaiting_items"] = False
    DRAFTS[chat_id] = draft
    await update.message.reply_markdown(draft_summary(draft))


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    number = " ".join(context.args or []).strip()
    if not number:
        await update.message.reply_text("Usage: /convert 104")
        return
    try:
        quote = find_document("quotes", number)
        if not quote:
            await update.message.reply_text(f"Quote {number} not found.")
            return
        invoice = convert_quote(quote)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_markdown("Converted.\n" + describe_doc("invoices", invoice))
    pdf = try_pdf("invoices", invoice)
    if pdf:
        label = invoice.get("invoiceId") or "invoice"
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf)
            path = tmp.name
        try:
            with open(path, "rb") as fh:
                await update.message.reply_document(document=fh, filename=f"{label}.pdf")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


async def cmd_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    name = " ".join(context.args or []).strip() or None
    try:
        rows = unpaid_invoices(name)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    if not rows:
        await update.message.reply_text("Nothing unpaid.")
        return
    lines = []
    for row in rows[:25]:
        lines.append(describe_doc("invoices", row).split("\n")[0])
    await update.message.reply_markdown("\n".join(lines))


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    number = " ".join(context.args or []).strip()
    if not number:
        await update.message.reply_text("Usage: /paid 88")
        return
    try:
        inv = find_document("invoices", number)
        if not inv:
            await update.message.reply_text(f"Invoice {number} not found.")
            return
        updated = mark_paid(inv)
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return
    await update.message.reply_markdown("Marked paid.\n" + describe_doc("invoices", updated))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    draft = DRAFTS.get(chat_id)

    if text.upper() == "YES":
        if not draft or not draft.get("kind") or not draft.get("items"):
            await update.message.reply_text("No draft to save. /quote or /invoice then /item.")
            return
        if not draft.get("client"):
            await update.message.reply_text("No client selected.")
            return
        try:
            doc = create_document(draft["kind"], draft["client"], draft["items"])
        except RuntimeError as exc:
            await update.message.reply_text(str(exc))
            return
        kind = "quotes" if draft["kind"] == "quote" else "invoices"
        DRAFTS.pop(chat_id, None)
        await update.message.reply_markdown("Saved.\n" + describe_doc(kind, doc))
        pdf = try_pdf(kind, doc)
        if pdf:
            label = doc.get("invoiceId") or doc.get("quoteId") or "document"
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf)
                path = tmp.name
            try:
                with open(path, "rb") as fh:
                    await update.message.reply_document(document=fh, filename=f"{label}.pdf")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return

    if draft and draft.get("awaiting_items"):
        try:
            rows = parse_item_chunks(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        draft.setdefault("items", []).extend(rows)
        draft["awaiting_items"] = False
        DRAFTS[chat_id] = draft
        await update.message.reply_markdown(draft_summary(draft))
        return


def main() -> None:
    require_env()
    log.info("Starting bot. SolidInvoice: %s", SI_URL)
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("client", cmd_client))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("quote", start_kind("quote")))
    app.add_handler(CommandHandler("invoice", start_kind("invoice")))
    app.add_handler(CommandHandler("item", cmd_item))
    app.add_handler(CommandHandler("convert", cmd_convert))
    app.add_handler(CommandHandler("unpaid", cmd_unpaid))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
