# bot.py
import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from dotenv import load_dotenv
from google_module import store_to_google_sheet, store_indicadores_to_drive_and_sheet
import nest_asyncio

nest_asyncio.apply()
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

user_sessions = {}  # {chat_id: {user_id: session_data}}
SESSION_TIMEOUT = 60  # for /asis
INDICADORES_TIMEOUT = 180  # for /indicadores (3 minutes)

async def asis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    if user_id in user_sessions[chat_id]:
        await update.message.reply_text("You already have an active session. Please finish it first.")
        return

    user_sessions[chat_id][user_id] = {
        "mode": "asis",
        "start_time": datetime.utcnow(),
        "photo": None,
        "metadata": None,
        "text": None,
        "timeout_task": asyncio.create_task(session_timeout_handler(chat_id, user_id, context))
    }

    await update.message.reply_text("Please send your image with a caption, and then send the name of the place (if not included in the caption). You have 1 minute.")

async def indicadores_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    if user_id in user_sessions[chat_id]:
        await update.message.reply_text("You already have an active session. Please finish it first.")
        return

    template = (
        "Por favor copia el siguiente formato y manda tus datos usándolo:\n\n"
        "---\n"
        "'Visitas Planeadas' : \n"
        "'Visitas Realizadas' : \n"
        "'OC Extra' : \n"
        "'Cotizaciones' : \n"
        "'Detalle de la venta' : \n"
        "'Clientes Nuevos' : \n"
        "---"
    )

    user_sessions[chat_id][user_id] = {
        "mode": "indicadores",
        "start_time": datetime.utcnow(),
        "parsed_data": None,
        "files": [],
        "timeout_task": None
    }

    await update.message.reply_text(template)

def parse_indicadores_text(text):
    fields = [
        "Visitas Planeadas", "Visitas Realizadas", "OC Extra",
        "Cotizaciones", "Detalle de la venta", "Clientes Nuevos"
    ]
    data = {}
    for line in text.splitlines():
        for field in fields:
            if line.strip().startswith(f"'{field}'"):
                try:
                    key, value = line.split(":", 1)
                    data[field] = value.strip()
                except Exception:
                    data[field] = ""
    return data if len(data) == len(fields) else None

async def session_timeout_handler(chat_id, user_id, context):
    await asyncio.sleep(SESSION_TIMEOUT)
    session = user_sessions.get(chat_id, {}).get(user_id)
    if session and session["photo"]:
        logging.info(f"Session timeout: storing image with caption only for user {user_id} in chat {chat_id}")
        user_record = {
            "user": context.bot.get_chat_member(chat_id, user_id).user,
            "image_file_id": session["metadata"]["file_id"],
            "caption": session["metadata"].get("caption"),
            "follow_up_text": "",
            "image_meta": session["metadata"]
        }
        await store_to_google_sheet(chat_id, user_record, context)
    user_sessions[chat_id].pop(user_id, None)

async def indicadores_file_timeout(chat_id, user_id, context):
    await asyncio.sleep(INDICADORES_TIMEOUT)
    session = user_sessions[chat_id].get(user_id)
    if session and session.get("mode") == "indicadores":
        await store_indicadores_to_drive_and_sheet(chat_id, user_id, session, context)
        user_sessions[chat_id].pop(user_id, None)

async def download_photo(file_id, context: ContextTypes.DEFAULT_TYPE):
    file = await context.bot.get_file(file_id)
    return await file.download_as_bytearray()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        if chat_id not in user_sessions or user_id not in user_sessions[chat_id]:
            return

        session = user_sessions[chat_id][user_id]
        if not update.message.photo:
            return

        photo = update.message.photo[-1]
        file_id = photo.file_id
        unique_id = photo.file_unique_id
        photo_data = await download_photo(file_id, context)
        message_date = update.message.date

        metadata = {
            "date": message_date.isoformat(),
            "file_id": file_id,
            "file_unique_id": unique_id,
            "caption": update.message.caption,
            "message_id": update.message.message_id,
            "username": update.effective_user.username,
            "photo_data": photo_data,
        }

        if session["mode"] == "asis":
            session["photo"] = file_id
            session["metadata"] = metadata
            session["timestamp"] = datetime.utcnow()
            await update.message.reply_text("✅ Photo received. Now please send the name of the place.")

        elif session["mode"] == "indicadores" and session.get("parsed_data"):
            session["files"].append({"file_id": file_id, "file_unique_id": unique_id, "data": photo_data})
            await update.message.reply_text("✅ Imagen recibida.")

    except Exception as e:
        logging.error(f"Error while handling photo from user {user_id}: {e}")
        await update.message.reply_text("❌ Error processing your photo. Please try again.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in user_sessions or user_id not in user_sessions[chat_id]:
        return

    session = user_sessions[chat_id][user_id]
    if session.get("mode") != "indicadores" or not session.get("parsed_data"):
        return

    file = update.message.document
    file_id = file.file_id
    file_data = await download_photo(file_id, context)

    session["files"].append({"file_id": file_id, "file_name": file.file_name, "data": file_data})
    await update.message.reply_text("✅ Archivo recibido.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in user_sessions or user_id not in user_sessions[chat_id]:
        return

    session = user_sessions[chat_id][user_id]
    if session["mode"] == "asis":
        if not session.get("photo"):
            return

        now = datetime.utcnow()
        if now - session["timestamp"] > timedelta(seconds=SESSION_TIMEOUT):
            user_sessions[chat_id].pop(user_id, None)
            return

        session["text"] = update.message.text
        user_record = {
            "user": update.effective_user,
            "image_file_id": session["metadata"]["file_id"],
            "caption": session["metadata"].get("caption"),
            "follow_up_text": session["text"],
            "image_meta": session["metadata"]
        }

        await store_to_google_sheet(chat_id, user_record, context)
        await update.message.reply_text("Saved to Google Drive")

    elif session["mode"] == "indicadores" and not session.get("parsed_data"):
        parsed = parse_indicadores_text(update.message.text)
        if parsed:
            session["parsed_data"] = parsed
            await update.message.reply_text("✅ Datos recibidos. ¿Tienes una orden de compra? Por favor envíala (foto o archivo) dentro de los próximos 3 minutos.")
            session["timeout_task"] = asyncio.create_task(indicadores_file_timeout(chat_id, user_id, context))
        else:
            await update.message.reply_text("❌ Formato incorrecto. Usa el formato proporcionado.")
        return

    if chat_id in user_sessions and user_id in user_sessions[chat_id]:
        if "timeout_task" in user_sessions[chat_id][user_id] and user_sessions[chat_id][user_id]["timeout_task"]:
            user_sessions[chat_id][user_id]["timeout_task"].cancel()
        user_sessions[chat_id].pop(user_id, None)

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("asis", asis_command))
    app.add_handler(CommandHandler("indicadores", indicadores_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.run_polling()

if __name__ == '__main__':
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
