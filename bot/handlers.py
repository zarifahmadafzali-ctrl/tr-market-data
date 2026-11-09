"""Telegram command and callback handlers."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.auth import can_use_bot, deny_message, is_admin
from bot.config import data_root, rate_limit_per_minute, sqlite_path
from bot.parsers import GetRequest, parse_get_args, supported_symbols
from bot.rate_limit import RateLimiter
from bot.services import (
    cancel_user_job,
    handle_get_request,
    job_progress,
    list_jobs,
    status_snapshot,
)

log = logging.getLogger(__name__)
_limiter = RateLimiter(rate_limit_per_minute())


async def _gate(update: Update) -> bool:
    user = update.effective_user
    uid = user.id if user else None
    if not can_use_bot(uid):
        if update.message:
            await update.message.reply_text(deny_message())
        elif update.callback_query:
            await update.callback_query.answer("Access denied", show_alert=True)
        return False
    if uid is not None and not _limiter.allow(uid):
        msg = "Rate limit exceeded. Please wait a minute."
        if update.message:
            await update.message.reply_text(msg)
        elif update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        return False
    return True


async def _send_offers(msg, context, files: list[dict]) -> None:
    """Send files preferring channel file_id, falling back to local path."""
    for f in files or []:
        label = f.get("label") or "file"
        file_id = f.get("file_id")
        path_str = f.get("path")
        source = f.get("source") or "local"

        if file_id:
            try:
                await context.bot.send_document(
                    chat_id=msg.chat_id,
                    document=file_id,
                    caption=f"{label} (from archive)",
                )
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("send by file_id failed")
                await msg.reply_text(f"Archive send failed for {label}: {e}")
                # fall through to local if available

        if not f.get("within_telegram_limit", True):
            await msg.reply_text(
                f"{label}: file is {f.get('size')} bytes — exceeds Telegram upload limit. "
                "Use server-side access or a future download link."
            )
            continue

        if path_str:
            path = Path(path_str)
            if path.is_file():
                try:
                    with path.open("rb") as fh:
                        await msg.reply_document(
                            document=fh, filename=path.name, caption=f"{label} (local)"
                        )
                except Exception as e:  # noqa: BLE001
                    log.exception("send failed")
                    await msg.reply_text(f"Failed to send {label}: {e}")
            else:
                await msg.reply_text(f"{label}: local file missing and no archive file_id.")
        elif not file_id:
            await msg.reply_text(f"{label}: no file available.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Status", callback_data="menu:status"),
                InlineKeyboardButton("Symbols", callback_data="menu:symbols"),
            ],
            [
                InlineKeyboardButton("Get data", callback_data="menu:get"),
                InlineKeyboardButton("Jobs", callback_data="menu:jobs"),
            ],
            [InlineKeyboardButton("Help", callback_data="menu:help")],
        ]
    )
    text = (
        "TR Market Data Bot\n\n"
        "Historical Dukascopy Tick + 1s OHLC archive.\n"
        "Admin-only access is enabled.\n\n"
        "Finished files are archived to a private Telegram channel "
        "so they survive server restarts.\n\n"
        "Use the menu or /help."
    )
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    text = (
        "Commands:\n"
        "/start — main menu\n"
        "/status — catalog + active jobs\n"
        "/symbols — supported symbols\n"
        "/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]\n"
        "/jobs — recent and active jobs\n"
        "/cancel JOB_ID — cancel a job (admin)\n"
        "/help — this message\n\n"
        "Examples:\n"
        "/get US30 2024-06-12 tick\n"
        "/get US30 2024-06-12 1s\n"
        "/get US30 2024-06-10 2024-06-12 both\n"
    )
    await update.message.reply_text(text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    snap = status_snapshot(sqlite_path())
    lines = [
        f"Catalog days: {snap['catalog_days']}",
        f"SUCCESS days: {snap['success_days']}",
        f"Active jobs: {snap['active_jobs']}",
    ]
    for j in snap["jobs"]:
        lines.append(f"  #{j['id']} {j['status']} {j['range']} {j['symbols']}")
    await update.message.reply_text("\n".join(lines) or "No data yet.")


async def cmd_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    syms = supported_symbols()
    await update.message.reply_text("Supported symbols / aliases:\n" + ", ".join(syms))


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    try:
        req = parse_get_args(context.args or [])
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    await update.message.reply_text(
        f"Checking catalog for {req.symbol} {req.start}→{req.end} ({req.kind})…"
    )
    result = handle_get_request(
        req,
        db_path=sqlite_path(),
        data_root=data_root(),
        run_immediately=False,
    )
    await _reply_get_result(update, context, result)


async def _reply_get_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict) -> None:
    lines = [
        f"Symbol: {result['symbol']}",
        f"Kind: {result['kind']}",
        f"Ready days: {', '.join(result['ready_days']) or 'none'}",
        f"Missing days: {', '.join(result['missing_days']) or 'none'}",
    ]
    if result.get("job_id"):
        lines.append(f"Queued job #{result['job_id']} (run worker to process).")
    msg = update.effective_message
    await msg.reply_text("\n".join(lines))
    await _send_offers(msg, context, result.get("files") or [])


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    jobs = list_jobs(sqlite_path())
    if not jobs:
        await update.message.reply_text("No jobs found.")
        return
    lines = []
    for j in jobs[:25]:
        lines.append(
            f"#{j['id']} {j['status']} {j['start_day']}→{j['end_day']} {j['symbols_json']}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("Only admins can cancel jobs.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancel JOB_ID")
        return
    try:
        job_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("JOB_ID must be an integer.")
        return
    out = cancel_user_job(sqlite_path(), job_id)
    if out.get("ok"):
        await update.message.reply_text(f"Job #{job_id} cancelled.")
    else:
        await update.message.reply_text(out.get("error") or "Cancel failed.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _gate(update):
        return
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""

    if data == "menu:status":
        snap = status_snapshot(sqlite_path())
        await q.edit_message_text(
            f"Catalog days: {snap['catalog_days']}\nSUCCESS: {snap['success_days']}\nActive jobs: {snap['active_jobs']}"
        )
        return
    if data == "menu:symbols":
        await q.edit_message_text("Symbols:\n" + ", ".join(supported_symbols()))
        return
    if data == "menu:help":
        await q.edit_message_text("Use /get SYMBOL YYYY-MM-DD [tick|1s|both]\n/jobs /status /cancel")
        return
    if data == "menu:jobs":
        jobs = list_jobs(sqlite_path())
        text = "\n".join(f"#{j['id']} {j['status']}" for j in jobs[:20]) or "No jobs"
        await q.edit_message_text(text)
        return
    if data == "menu:get":
        buttons = [
            [InlineKeyboardButton("US30", callback_data="get:sym:US30")],
            [InlineKeyboardButton("EURUSD", callback_data="get:sym:EURUSD")],
            [InlineKeyboardButton("XAUUSD", callback_data="get:sym:XAUUSD")],
        ]
        await q.edit_message_text("Select symbol:", reply_markup=InlineKeyboardMarkup(buttons))
        return
    if data.startswith("get:sym:"):
        sym = data.split(":", 2)[2]
        context.user_data["get_symbol"] = sym
        today = date.today()
        buttons = []
        for i in range(0, 7):
            d = today - timedelta(days=i + 1)
            buttons.append(
                [InlineKeyboardButton(d.isoformat(), callback_data=f"get:day:{d.isoformat()}")]
            )
        await q.edit_message_text(
            f"Symbol {sym}. Select a day (or use /get {sym} YYYY-MM-DD tick):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if data.startswith("get:day:"):
        day_s = data.split(":", 2)[2]
        sym = context.user_data.get("get_symbol") or "US30"
        context.user_data["get_day"] = day_s
        buttons = [
            [
                InlineKeyboardButton("Tick", callback_data="get:kind:tick"),
                InlineKeyboardButton("1s", callback_data="get:kind:1s"),
                InlineKeyboardButton("Both", callback_data="get:kind:both"),
            ]
        ]
        await q.edit_message_text(
            f"{sym} {day_s} — select data type:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if data.startswith("get:kind:"):
        kind = data.split(":", 2)[2]
        sym = context.user_data.get("get_symbol") or "US30"
        day_s = context.user_data.get("get_day")
        if not day_s:
            await q.edit_message_text("Missing day. Start again with Get data.")
            return
        try:
            req = GetRequest(
                symbol=sym,
                start=date.fromisoformat(day_s),
                end=date.fromisoformat(day_s),
                kind=kind,  # type: ignore[arg-type]
            )
        except Exception as e:  # noqa: BLE001
            await q.edit_message_text(str(e))
            return
        result = handle_get_request(
            req, db_path=sqlite_path(), data_root=data_root(), run_immediately=False
        )
        lines = [
            f"Symbol: {result['symbol']}",
            f"Kind: {result['kind']}",
            f"Ready: {', '.join(result['ready_days']) or 'none'}",
            f"Missing: {', '.join(result['missing_days']) or 'none'}",
        ]
        if result.get("job_id"):
            lines.append(f"Queued job #{result['job_id']}")
        await q.edit_message_text("\n".join(lines))
        # Send files if ready
        class _FakeMsg:
            def __init__(self, chat_id):
                self.chat_id = chat_id

            async def reply_text(self, text):
                await context.bot.send_message(chat_id=self.chat_id, text=text)

            async def reply_document(self, **kwargs):
                await context.bot.send_document(chat_id=self.chat_id, **kwargs)

        fake = _FakeMsg(q.message.chat_id)
        await _send_offers(fake, context, result.get("files") or [])
