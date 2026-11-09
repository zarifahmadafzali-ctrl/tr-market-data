"""Telegram command and callback handlers."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.auth import can_use_bot, deny_message, is_admin
from bot.backfill import (
    BackfillError,
    backfill_context_summary,
    parse_archive_document,
    register_legacy_artifact,
)
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
        "/status — catalog + active jobs + archive counts\n"
        "/symbols — supported symbols\n"
        "/get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]\n"
        "/jobs — recent and active jobs\n"
        "/cancel JOB_ID — cancel a job (admin)\n"
        "/backfill [SYMBOL YYYY-MM-DD] — index existing Telegram archive files (admin)\n"
        "/help — this message\n\n"
        "Examples:\n"
        "/get US30 2026-11-08 tick\n"
        "/backfill US30 2026-11-07\n"
        "Then forward the 3 archive documents from the private channel.\n"
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
    arch = snap.get("archive") or {}
    if arch:
        lines.append(
            f"Archive INDEXED: {arch.get('indexed_artifacts', 0)}  pending: {arch.get('pending', 0)}"
        )
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
        requester_chat_id=(update.effective_chat.id if update.effective_chat else None),
        requester_user_id=(update.effective_user.id if update.effective_user else None),
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

    # Deliver ready files (archive file_id first, then local disk)
    files = result.get("files") or []
    log.info("delivering %s file(s) for %s", len(files), result.get("symbol"))
    if result.get("archive_partial_days"):
        await msg.reply_text(
            "Archive incomplete for: "
            + ", ".join(result["archive_partial_days"])
            + " — local/archive upload continues in background."
        )
    if not files and result.get("ready_days"):
        await msg.reply_text(
            "Data is ready in catalog but no files could be resolved for upload. "
            "Please notify admin (path bug)."
        )
    for f in files:
        try:
            if f.get("source") == "telegram_archive" and f.get("file_id"):
                await msg.reply_document(
                    document=f["file_id"],
                    filename=f.get("file_name") or "data.bin",
                    caption=f.get("label") or "",
                )
                continue
            path = Path(f["path"])
            if not f.get("within_telegram_limit"):
                await msg.reply_text(
                    f"{f['label']}: file is {f['size']} bytes — exceeds Telegram upload limit. "
                    "Use server-side access or a future download link."
                )
                continue
            if not path.is_file():
                await msg.reply_text(f"Missing local file for {f.get('label')}")
                continue
            with path.open("rb") as fh:
                await msg.reply_document(document=fh, filename=path.name, caption=f["label"])
        except Exception as e:  # noqa: BLE001
            log.exception("send failed")
            await msg.reply_text(f"Failed to send {f.get('label')}: {e}")




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
            req,
            db_path=sqlite_path(),
            data_root=data_root(),
            run_immediately=False,
            requester_chat_id=(q.message.chat_id if q.message else None),
            requester_user_id=(q.from_user.id if q.from_user else None),
        )
        lines = [
            f"Symbol: {result['symbol']}",
            f"Kind: {result['kind']}",
            f"Ready: {', '.join(result['ready_days']) or 'none'}",
            f"Missing: {', '.join(result['missing_days']) or 'none'}",
        ]
        if result.get("job_id"):
            lines.append(f"Queued job #{result['job_id']}")
        if result.get("archive_partial_days"):
            lines.append("Archive pending: " + ", ".join(result["archive_partial_days"]))
        await q.edit_message_text("\n".join(lines))
        for f in result.get("files") or []:
            try:
                if f.get("source") == "telegram_archive" and f.get("file_id"):
                    await context.bot.send_document(
                        chat_id=q.message.chat_id,
                        document=f["file_id"],
                        filename=f.get("file_name") or "data.bin",
                        caption=f.get("label") or "",
                    )
                    continue
                path = Path(f["path"])
                if f.get("within_telegram_limit") and path.is_file():
                    with path.open("rb") as fh:
                        await context.bot.send_document(
                            chat_id=q.message.chat_id,
                            document=fh,
                            filename=path.name,
                            caption=f["label"],
                        )
            except Exception as e:  # noqa: BLE001
                await context.bot.send_message(
                    chat_id=q.message.chat_id, text=f"Send failed: {e}"
                )


async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: set symbol/day context and/or instructions for forwarding archive docs."""
    if not await _gate(update):
        return
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("Only admins can backfill the archive index.")
        return

    args = context.args or []
    if len(args) >= 2:
        try:
            from bot.parsers import parse_date
            from downloader.symbols import normalize_symbol
            sym = normalize_symbol(args[0])
            day = parse_date(args[1]).isoformat()
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        context.user_data["backfill_symbol"] = sym
        context.user_data["backfill_day"] = day
        context.user_data["backfill_mode"] = True
        await update.message.reply_text(
            f"Backfill context set: {sym} {day}.\n"
            "Forward the archive documents (ticks.parquet, bars_1s.parquet, "
            "metadata.json) from the private channel to this chat.\n"
            "Each file will be indexed into the GitHub manifest without re-upload.\n"
            "Send /backfill clear to exit."
        )
        return

    if args and args[0].lower() in ("clear", "off", "stop", "done"):
        context.user_data.pop("backfill_symbol", None)
        context.user_data.pop("backfill_day", None)
        context.user_data.pop("backfill_mode", None)
        await update.message.reply_text("Backfill context cleared.")
        return

    context.user_data["backfill_mode"] = True
    await update.message.reply_text(
        "Backfill mode on.\n"
        "Option A: /backfill SYMBOL YYYY-MM-DD then forward files.\n"
        "Option B: forward files that already have full captions "
        "(dukascopy|SYMBOL|YYYY-MM-DD|ticks_parquet).\n"
        f"{backfill_context_summary(context.user_data)}\n"
        "Send /backfill clear when finished."
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: register forwarded archive documents into the durable index."""
    if not await _gate(update):
        return
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return
    msg = update.message
    if not msg or not msg.document:
        return

    # Only process when in backfill mode or caption looks like an archive key
    caption = msg.caption or ""
    in_mode = bool(context.user_data.get("backfill_mode"))
    looks_like_archive = "|" in caption or (msg.document.file_name or "").endswith(
        (".parquet", ".json")
    )
    if not in_mode and not looks_like_archive:
        return

    doc = msg.document
    try:
        parsed = parse_archive_document(
            file_id=doc.file_id,
            file_name=doc.file_name,
            caption=caption,
            file_unique_id=getattr(doc, "file_unique_id", None),
            message_id=msg.message_id,
            chat_id=msg.chat_id,
            file_size=getattr(doc, "file_size", None),
            hint_symbol=context.user_data.get("backfill_symbol"),
            hint_day=context.user_data.get("backfill_day"),
        )
    except BackfillError as e:
        await msg.reply_text(f"Backfill parse error: {e}")
        return

    result = register_legacy_artifact(sqlite_path(), parsed, write_manifest=True)
    if not result.get("ok"):
        await msg.reply_text(
            f"Backfill failed ({result.get('action')}): {result.get('error')}"
        )
        return

    extra = ""
    if result.get("manifest_enabled") and not result.get("manifest_written"):
        if result.get("action") == "already_indexed":
            extra = " (already in index)"
        else:
            extra = " (SQLite only — manifest not updated)"
    elif result.get("manifest_written"):
        extra = " + GitHub manifest"

    await msg.reply_text(
        f"Indexed {result['symbol']} {result['day']} {result['file_name']}\n"
        f"action={result['action']} key={result['artifact_key']}{extra}"
    )


