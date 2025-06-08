# bot.py

import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from datetime import datetime, timedelta
import logging
import asyncio
from dotenv import load_dotenv
from google_module import store_to_google_sheet  # your custom module
import nest_asyncio

nest_asyncio.apply()

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

# Temporary in-memory image cache
image_cache = {}

async def download_photo(file_id, context: ContextTypes.DEFAULT_TYPE):
    """Download photo from Telegram servers"""
    file = await context.bot.get_file(file_id)
    return await file.download_as_bytearray()

# Handle images
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not update.message.photo:
        return

    photo = update.message.photo[-1]  # highest resolution
    file_id = photo.file_id
    unique_id = photo.file_unique_id

    photo_data = await download_photo(file_id, context)

    metadata = {
        "date": update.message.date.isoformat(),
        "file_id": file_id,
        "file_unique_id": unique_id,
        "caption": update.message.caption,
        "message_id": update.message.message_id,
        "username": update.effective_user.username,
        "photo_data": photo_data,  # Store the actual image data
    }

    if chat_id not in image_cache:
        image_cache[chat_id] = {}

    image_cache[chat_id][user_id] = {
        "photo_file_id": file_id,
        "file_unique_id": unique_id,
        "timestamp": datetime.utcnow(),
        "caption": update.message.caption,
        "image_meta": metadata,
    }

    logging.info(f"Cached image from user {user_id} in chat {chat_id}")

# Handle follow-up text
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    now = datetime.utcnow()

    if chat_id not in image_cache or user_id not in image_cache[chat_id]:
        return

    cached = image_cache[chat_id][user_id]
    if now - cached["timestamp"] > timedelta(seconds=30):
        del image_cache[chat_id][user_id]
        return

    user_record = {
        "username": update.effective_user.username,
        "image_file_id": cached["photo_file_id"],
        "caption": cached["caption"],
        "follow_up_text": update.message.text,
        "image_meta": cached["image_meta"],
        "text_timestamp": update.message.date.isoformat()
    }

    store_to_google_sheet(chat_id, user_record)
    del image_cache[chat_id][user_id]

    logging.info(f"Stored follow-up for user {user_id} in chat {chat_id}")

# Main entrypoint
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.run_polling()

# Run the bot
if __name__ == '__main__':
    try:
        loop = asyncio.get_running_loop()
        import nest_asyncio
        nest_asyncio.apply()  # Patch the existing loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(main())
