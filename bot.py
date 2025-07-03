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

user_sessions = {}  # /asis sessions
indicadores_sessions = {}  # /indicadores sessions

SESSION_TIMEOUT = 60  # seconds for /asis
INDICADORES_TIMEOUT = 180  # seconds for /indicadores

# ---------------- /ASIS Logic ------------------

async def asis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    if user_id in user_sessions[chat_id]:
        await update.message.reply_text("You already have an active /asis session.")
        return

    user_sessions[chat_id][user_id] = {
        "start_time": datetime.utcnow(),
        "photo": None,
        "metadata": None,
        "text": None,
        "timeout_task": asyncio.create_task(session_timeout_handler(chat_id, user_id, context))
    }

    await update.message.reply_text("Please send your image with a caption, and then send the name of the place (if not included in the caption). You have 1 minute.")

async def session_timeout_handler(chat_id, user_id, context):
    await asyncio.sleep(SESSION_TIMEOUT)
    session = user_sessions.get(chat_id, {}).get(user_id)
    if session and session["photo"]:
        logging.info(f"Session timeout: storing image with caption only for user {user_id} in chat {chat_id}")
        user_record = {
            "user": (await context.bot.get_chat_member(chat_id, user_id)).user,
            "image_file_id": session["metadata"]["file_id"],
            "caption": session["metadata"].get("caption"),
            "follow_up_text": "",
            "image_meta": session["metadata"]
        }
        await store_to_google_sheet(chat_id, user_record, context)
        await context.bot.send_message(chat_id, "✅ Imagen almacenada automáticamente al agotar el tiempo.")
    user_sessions[chat_id].pop(user_id, None)

async def download_photo(file_id, context):
    file = await context.bot.get_file(file_id)
    return await file.download_as_bytearray()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id in user_sessions and user_id in user_sessions[chat_id]:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        photo_data = await download_photo(file_id, context)
        metadata = {
            "date": update.message.date.isoformat(),
            "file_id": file_id,
            "file_unique_id": photo.file_unique_id,
            "caption": update.message.caption,
            "message_id": update.message.message_id,
            "username": update.effective_user.username,
            "photo_data": photo_data,
        }

        session = user_sessions[chat_id][user_id]
        session["photo"] = file_id
        session["metadata"] = metadata
        session["timestamp"] = datetime.utcnow()

        await update.message.reply_text("✅ Photo received. Now please send the name of the place.")
    elif chat_id in indicadores_sessions and user_id in indicadores_sessions[chat_id]:
        session = indicadores_sessions[chat_id][user_id]
        photo = update.message.photo[-1]
        file_id = photo.file_id
        photo_data = await download_photo(file_id, context)
        session["files"].append({"data": photo_data, "file_name": None})
        await update.message.reply_text("📷 Imagen de indicadores recibida.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id in user_sessions and user_id in user_sessions[chat_id]:
        session = user_sessions[chat_id][user_id]
        if not session.get("photo"):
            return

        if datetime.utcnow() - session["timestamp"] > timedelta(seconds=SESSION_TIMEOUT):
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
        await update.message.reply_text("✅ Información de asistencia guardada correctamente.")

        if session.get("timeout_task"):
            session["timeout_task"].cancel()
        user_sessions[chat_id].pop(user_id, None)
    elif chat_id in indicadores_sessions and user_id in indicadores_sessions[chat_id]:
        session = indicadores_sessions[chat_id][user_id]
        text = update.message.text
        parsed = {}
        try:
            for line in text.splitlines():
                if ':' in line:
                    key, value = line.split(':', 1)
                    parsed[key.strip(" '")] = value.strip(" '")
            session["parsed_data"] = parsed
            await update.message.reply_text("✅ Formato recibido. Puedes enviar archivos o imágenes ahora. Usa /done para terminar.")
        except Exception as e:
            logging.error(f"Error parsing indicadores text: {e}")
            await update.message.reply_text("❌ Error en el formato. Asegúrate de usar el formato indicado.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id in indicadores_sessions and user_id in indicadores_sessions[chat_id]:
        file = update.message.document
        if not file:
            return
        file_id = file.file_id
        data = await download_photo(file_id, context)
        indicadores_sessions[chat_id][user_id]["files"].append({"data": data, "file_name": file.file_name})
        await update.message.reply_text("📎 Archivo recibido.")

async def indicadores_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in indicadores_sessions:
        indicadores_sessions[chat_id] = {}

    if user_id in indicadores_sessions[chat_id]:
        await update.message.reply_text("❗ Ya tienes una sesión de /indicadores activa. Usa /done para finalizarla.")
        return

    indicadores_sessions[chat_id][user_id] = {
        "start_time": datetime.utcnow(),
        "parsed_data": {},
        "files": [],
        "timeout_task": asyncio.create_task(indicadores_timeout_handler(chat_id, user_id, context))
    }

    await update.message.reply_text(
        "Por favor copia el siguiente formato y manda tus datos usándolo:\n---\n'Visitas Planeadas':\n'Visitas Realizadas':\n'OC Extra':\n'Cotizaciones':\n'Detalle de la venta':\n'Clientes Nuevos':\n---"
    )

async def indicadores_timeout_handler(chat_id, user_id, context):
    await asyncio.sleep(INDICADORES_TIMEOUT)
    session = indicadores_sessions.get(chat_id, {}).get(user_id)
    if session:
        await store_indicadores_to_drive_and_sheet(chat_id, user_id, session, context)
        await context.bot.send_message(chat_id, f"⏳ Sesión de /indicadores terminada por inactividad y guardada.")
        indicadores_sessions[chat_id].pop(user_id, None)

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if chat_id in indicadores_sessions and user_id in indicadores_sessions[chat_id]:
        session = indicadores_sessions[chat_id][user_id]
        if session.get("timeout_task"):
            session["timeout_task"].cancel()
        await store_indicadores_to_drive_and_sheet(chat_id, user_id, session, context)
        indicadores_sessions[chat_id].pop(user_id, None)
        await update.message.reply_text("✅ Sesión de /indicadores finalizada manualmente y guardada.")
    else:
        await update.message.reply_text("ℹ️ No tienes una sesión de /indicadores activa.")

async def listo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await done_command(update, context)  # Alias to /done

# ---------------- Main ------------------

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("asis", asis_command))
    app.add_handler(CommandHandler("indicadores", indicadores_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("listo", listo_command))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.run_polling()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass