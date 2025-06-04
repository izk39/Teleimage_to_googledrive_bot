import os
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Load .env variables
load_dotenv()

SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

# Authenticate and build services
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

sheet_cache = {}

def get_or_create_sheet_for_chat(chat_id):
    if chat_id in sheet_cache:
        return sheet_cache[chat_id]

    query = f"'{ROOT_FOLDER_ID}' in parents and name = '{chat_id}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

    if files:
        sheet_id = files[0]['id']
    else:
        spreadsheet_body = {
            'properties': {'title': str(chat_id)},
        }
        # File creation does not take "parents" in Sheets API; you use Drive API to move it later if needed
        spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet_body).execute()
        sheet_id = spreadsheet['spreadsheetId']

        # Move spreadsheet to desired folder
        drive_service.files().update(
            fileId=sheet_id,
            addParents=ROOT_FOLDER_ID,
            removeParents='root',
            fields='id, parents'
        ).execute()

    sheet_cache[chat_id] = sheet_id
    return sheet_id

def store_to_google_sheet(chat_id, user_data):
    sheet_id = get_or_create_sheet_for_chat(chat_id)
    sheet_range = 'A1'  # We'll append, so the range doesn't matter

    row = [
        user_data.get("username"),
        user_data.get("image_file_id"),
        user_data.get("caption"),
        user_data.get("follow_up_text"),
        user_data.get("image_meta", {}).get("date"),
        user_data.get("image_meta", {}).get("file_unique_id"),
        user_data.get("image_meta", {}).get("message_id"),
        user_data.get("text_timestamp")
    ]

    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=sheet_range,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]}
        ).execute()
    except HttpError as e:
        print(f"Failed to append row: {e}")
