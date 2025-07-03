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
        print(f"Error formatting datetime: {e}")
        return dt_str

def get_or_create_folder(parent_id, folder_name):
    folder_name = "".join(c for c in folder_name if c not in r'\\/:*?"<>|')
    query = f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder'"
    try:
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = results.get('files', [])
        if folders:
            return folders[0]['id']
    except Exception as e:
        print(f"Error searching for folder: {e}")
    try:
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id, name').execute()
        return folder['id']
    except Exception as e:
        print(f"Error creating folder: {e}")
        raise

def get_or_create_sheet(chat_id, chat_name, mode="asis"):
    key = f"{chat_id}_{mode}"
    if key in sheet_cache:
        return sheet_cache[key]

    sheet_name = f"{chat_name}_{'asistencias' if mode == 'asis' else 'indicadores'}"
    chat_folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)

    query = f"'{chat_folder_id}' in parents and name = '{sheet_name}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

    if files:
        sheet_id = files[0]['id']
    else:
        spreadsheet_body = {'properties': {'title': sheet_name}}
        spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet_body).execute()
        sheet_id = spreadsheet['spreadsheetId']
        drive_service.files().update(
            fileId=sheet_id,
            addParents=chat_folder_id,
            removeParents='root',
            fields='id, parents'
        ).execute()

    sheet_cache[key] = sheet_id
    return sheet_id

def upload_image_to_drive(image_data, filename, folder_id):
    filename = "".join(c for c in filename if c not in r'\\/:*?"<>|')
    if isinstance(image_data, bytearray):
        image_data = bytes(image_data)

    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }

    media = MediaIoBaseUpload(io.BytesIO(image_data), mimetype='image/jpeg', resumable=True)
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, webViewLink'
    ).execute()

    drive_service.permissions().create(
        fileId=file['id'],
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    return file['webViewLink']

async def store_to_google_sheet(chat_id, user_data, context):
    chat_name = await get_chat_name(chat_id, context)
    user_name = get_user_name(user_data['user'])

    sheet_id = get_or_create_sheet(chat_id, chat_name, mode="asis")
    chat_folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)
    asis_folder_id = get_or_create_folder(chat_folder_id, "asis")

    image_data = user_data.get("image_meta", {}).get("photo_data")
    if image_data:
        timestamp = user_data.get("image_meta", {}).get("date", "")
        formatted_date = format_datetime_for_sheets(timestamp)
        filename = f"{user_name}_{formatted_date.replace('/', '-').replace(':', '-')}.jpg"
        image_url = upload_image_to_drive(image_data, filename, asis_folder_id)
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

    try:
        headers = ["User", "Caption", "Follow-up Text", "Date", "Image URL", "Image"]
        result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range='A1:Z1').execute()

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
    except HttpError as e:
        print(f"Failed to append row: {e}")

async def store_indicadores_to_drive_and_sheet(chat_id, user_id, session, context):
    chat_name = await get_chat_name(chat_id, context)
    user = (await context.bot.get_chat_member(chat_id, user_id)).user
    user_name = get_user_name(user)

    sheet_id = get_or_create_sheet(chat_id, chat_name, mode="indicadores")
    chat_folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)
    indicadores_folder_id = get_or_create_folder(chat_folder_id, "indicadores")

    file_links = []
    for idx, f in enumerate(session["files"]):
        filename = f.get("file_name") or f"upload_{idx}.jpg"
        link = upload_image_to_drive(f["data"], f"{user_name}_{filename}", indicadores_folder_id)
        file_links.append(link)

    indicators = session["parsed_data"]
    row = [user_name] + [indicators.get(k, "") for k in [
        "Visitas Planeadas", "Visitas Realizadas", "OC Extra", "Cotizaciones", "Detalle de la venta", "Clientes Nuevos"
    ]] + file_links

    try:
        headers = [
            "Usuario", "Visitas Planeadas", "Visitas Realizadas", "OC Extra",
            "Cotizaciones", "Detalle de la venta", "Clientes Nuevos"
        ] + [f"Archivo {i+1}" for i in range(len(file_links))]

        result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range='A1:Z1').execute()
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
    except HttpError as e:
        print(f"Error saving indicadores data: {e}")
