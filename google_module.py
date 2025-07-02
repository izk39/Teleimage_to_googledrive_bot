# google_module.py
import os
import io
import pytz
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")

SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

sheet_cache = {}
folder_cache = {}

def sanitize_filename(name):
    return "".join(c for c in name if c not in r'\\/:*?"<>|')

async def get_chat_name(chat_id, context):
    try:
        chat = await context.bot.get_chat(chat_id)
        return chat.title or f"Chat_{chat_id}"
    except Exception as e:
        print(f"Error getting chat name: {e}")
        return f"Chat_{chat_id}"

def get_user_name(user):
    if user.username:
        return f"@{user.username}"
    elif user.first_name or user.last_name:
        return f"{user.first_name or ''} {user.last_name or ''}".strip()
    else:
        return f"User_{user.id}"

def format_datetime_for_sheets(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        local_tz = pytz.timezone('America/Mexico_City')
        local_dt = dt.astimezone(local_tz)
        return local_dt.strftime('%m/%d/%Y %H:%M:%S')
    except Exception as e:
        print(f"Datetime formatting error: {e}")
        return dt_str

def get_or_create_folder(parent_id, folder_name):
    folder_name = sanitize_filename(folder_name)
    query = f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder'"
    try:
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = results.get('files', [])
        if folders:
            return folders[0]['id']
    except Exception as e:
        print(f"[ERROR] Error searching folder: {e}")

    try:
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        return folder['id']
    except Exception as e:
        print(f"[ERROR] Error creating folder: {e}")
        raise

def get_or_create_sheet(chat_id, sheet_name):
    key = f"{chat_id}:{sheet_name}"
    if key in sheet_cache:
        return sheet_cache[key]

    folder_id = get_or_create_folder(ROOT_FOLDER_ID, sheet_name)
    query = f"'{folder_id}' in parents and name = '{sheet_name}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

    if files:
        sheet_id = files[0]['id']
    else:
        spreadsheet = sheets_service.spreadsheets().create(
            body={'properties': {'title': sheet_name}}
        ).execute()
        sheet_id = spreadsheet['spreadsheetId']
        drive_service.files().update(
            fileId=sheet_id,
            addParents=folder_id,
            removeParents='root',
            fields='id, parents'
        ).execute()

    sheet_cache[key] = sheet_id
    return sheet_id

def upload_image_to_drive(image_data, filename, folder_id):
    filename = sanitize_filename(filename)
    if isinstance(image_data, bytearray):
        image_data = bytes(image_data)

    metadata = {
        'name': filename,
        'parents': [folder_id]
    }

    media = MediaIoBaseUpload(io.BytesIO(image_data), mimetype='application/octet-stream', resumable=True)
    file = drive_service.files().create(body=metadata, media_body=media, fields='id, webViewLink').execute()

    drive_service.permissions().create(
        fileId=file['id'],
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    return file['webViewLink']

async def store_to_google_sheet(chat_id, user_data, context):
    chat_name = await get_chat_name(chat_id, context)
    user_name = get_user_name(user_data['user'])
    sheet_id = get_or_create_sheet(chat_id, chat_name)
    folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)

    image_data = user_data.get("image_meta", {}).get("photo_data")
    if image_data:
        timestamp = user_data.get("image_meta", {}).get("date", "")
        formatted_date = format_datetime_for_sheets(timestamp)
        filename = f"{user_name}_{formatted_date.replace('/', '-').replace(':', '-')}.jpg"
        image_url = upload_image_to_drive(image_data, filename, folder_id)
    else:
        image_url = "No image"

    row = [
        user_name,
        user_data.get("caption", ""),
        user_data.get("follow_up_text", ""),
        format_datetime_for_sheets(user_data.get("image_meta", {}).get("date", "")),
        image_url,
        f'=IMAGE("{image_url}")' if image_url != "No image" else "No image"
    ]

    headers = ["User", "Caption", "Follow-up Text", "Date", "Image URL", "Image"]

    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range='A1:Z1'
    ).execute()

    if 'values' not in result:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range='A1',
            valueInputOption="RAW",
            body={"values": [headers]}
        ).execute()

    sheets_service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range='A1',
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()

async def store_indicadores_to_drive_and_sheet(chat_id, user_id, session, context):
    chat_name = await get_chat_name(chat_id, context)
    user = await context.bot.get_chat_member(chat_id, user_id)
    user_name = get_user_name(user.user)

    folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)
    indicadores_folder = get_or_create_folder(folder_id, "Indicadores")
    sheet_id = get_or_create_sheet(chat_id * 1000 + 1, f"{chat_name}_Indicadores")

    file_links = []
    for idx, f in enumerate(session.get("files", [])):
        filename = f.get("file_name", f"{user_name}_file_{idx}.bin")
        link = upload_image_to_drive(f["data"], filename, indicadores_folder)
        file_links.append(link)

    row = [
        user_name,
        *[session["parsed_data"].get(k, "") for k in [
            "Visitas Planeadas", "Visitas Realizadas", "OC Extra",
            "Cotizaciones", "Detalle de la venta", "Clientes Nuevos"
        ]],
        ", ".join(file_links) if file_links else "No OC Adjunta"
    ]

    headers = [
        "Usuario", "Visitas Planeadas", "Visitas Realizadas", "OC Extra",
        "Cotizaciones", "Detalle de la venta", "Clientes Nuevos", "Links Adjuntos"
    ]

    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range='A1:Z1'
    ).execute()

    if 'values' not in result:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range='A1',
            valueInputOption="RAW",
            body={"values": [headers]}
        ).execute()

    sheets_service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range='A1',
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()

    await context.bot.send_message(chat_id, f"✅ Indicadores guardados correctamente.\nLinks: {' | '.join(file_links) if file_links else 'No OC enviada'}")
