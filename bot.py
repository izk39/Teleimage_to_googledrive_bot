# bot.py /asis added
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
from google_module import store_to_google_sheet
import nest_asyncio

nest_asyncio.apply()
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

# Temporary in-memory cache for /asis sessions
user_sessions = {}  # {chat_id: {user_id: session_data}}

SESSION_TIMEOUT = 60  # seconds

async def asis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    if user_id in user_sessions[chat_id]:
        await update.message.reply_text("You already have an active session. Please finish it first.")
        return

    # Initialize session
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
            "user": context.bot.get_chat_member(chat_id, user_id).user,
            "image_file_id": session["metadata"]["file_id"],
            "caption": session["metadata"].get("caption"),
            "follow_up_text": "",
            "image_meta": session["metadata"]
        }
        await store_to_google_sheet(chat_id, user_record, context)
    user_sessions[chat_id].pop(user_id, None)

async def download_photo(file_id, context: ContextTypes.DEFAULT_TYPE):
    file = await context.bot.get_file(file_id)
    return await file.download_as_bytearray()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        if chat_id not in user_sessions or user_id not in user_sessions[chat_id]:
            return  # Not in session

        if not update.message.photo:
            return

        session = user_sessions[chat_id][user_id]
        photo = update.message.photo[-1]  # highest resolution
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

        session["photo"] = file_id
        session["metadata"] = metadata
        session["timestamp"] = datetime.utcnow()

        logging.info(f"Photo received from user {user_id} in chat {chat_id}")
        await update.message.reply_text("✅ Photo received. Now please send the name of the place.")

    except Exception as e:
        logging.error(f"Error while handling photo from user {user_id}: {e}")
        await update.message.reply_text("❌ Error processing your photo. Please try /asis again.")

    finally:
        # Don't clear the session here — we still expect a follow-up text
        pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if chat_id not in user_sessions or user_id not in user_sessions[chat_id]:
            return  # Not in session

        session = user_sessions[chat_id][user_id]
        if not session.get("photo"):
            return  # Photo not sent yet

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
    except Exception as e:
        logging.error(f"Google Drive error: {e}")
        await update.message.reply_text("❌ Failed to save. Please try /asis again.")

    finally:  # Changed to finally to ensure cleanup
        if chat_id in user_sessions and user_id in user_sessions[chat_id]:
            if "timeout_task" in user_sessions[chat_id][user_id]:
                user_sessions[chat_id][user_id]["timeout_task"].cancel()
            user_sessions[chat_id].pop(user_id, None)
    
    logging.info(f"Stored complete entry for user {user_id} in chat {chat_id}")

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("asis", asis_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.run_polling()

if __name__ == '__main__':
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
