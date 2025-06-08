# google_module.py (updated)
import os
import io
import pytz
from datetime import datetime, timezone  # ADD THIS IMPORT
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")

if not SERVICE_ACCOUNT_FILE:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_FILE in environment.")

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

async def get_chat_name(chat_id, context):  # Make this async
    """Get chat title from Telegram API"""
    try:
        chat = await context.bot.get_chat(chat_id)  # Add await
        return chat.title or f"Chat_{chat_id}"
    except Exception as e:
        print(f"Error getting chat name: {e}")
        return f"Chat_{chat_id}"

def get_user_name(user):
    """Get user's display name"""
    if user.username:
        return f"@{user.username}"
    elif user.first_name or user.last_name:
        return f"{user.first_name or ''} {user.last_name or ''}".strip()
    else:
        return f"User_{user.id}"

def format_datetime_for_sheets(dt_str):
    """Convert ISO datetime string to Google Sheets datetime format"""
    try:
        dt = datetime.fromisoformat(dt_str)
        # Format: MM/DD/YYYY HH:MM:SS (24-hour format)
        local_tz = pytz.timezone('America/Mexico_City') 
        local_dt = dt.astimezone(local_tz)

        return local_dt.strftime('%m/%d/%Y %H:%M:%S')
    except (ValueError, TypeError) as e:
        print(f"Error formatting datetime: {e}")
        return dt_str  # Return original if parsing fails

def get_or_create_folder(parent_id, folder_name):
    """Get or create a folder with the given name"""
    # Sanitize folder name to remove invalid characters
    folder_name = "".join(c for c in folder_name if c not in r'\/:*?"<>|')
    
    query = f"'{parent_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder'"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    folders = results.get('files', [])

    if folders:
        return folders[0]['id']
    else:
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        return folder['id']

def get_or_create_sheet(chat_id, chat_name):
    """Get or create a spreadsheet with the chat name and ID"""
    if chat_id in sheet_cache:
        return sheet_cache[chat_id]

    # First create/get the chat folder
    chat_folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)
    
    # Check for existing sheet in this folder
    query = f"'{chat_folder_id}' in parents and name = '{chat_name}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

    if files:
        sheet_id = files[0]['id']
    else:
        spreadsheet_body = {
            'properties': {'title': chat_name},
        }
        spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet_body).execute()
        sheet_id = spreadsheet['spreadsheetId']

        # Move spreadsheet to the chat folder
        drive_service.files().update(
            fileId=sheet_id,
            addParents=chat_folder_id,
            removeParents='root',
            fields='id, parents'
        ).execute()

        # Format the header row
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {"red": 0.8, "green": 0.8, "blue": 0.8},
                                    "textFormat": {"bold": True}
                                }
                            },
                            "fields": "userEnteredFormat"
                        }
                    }
                ]
            }
        ).execute()

    sheet_cache[chat_id] = sheet_id
    return sheet_id

def upload_image_to_drive(image_data, filename, folder_id):
    """Upload image to specific folder and return shareable link"""
    # Sanitize filename
    filename = "".join(c for c in filename if c not in r'\/:*?"<>|')
    
    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }
    
    media = MediaIoBaseUpload(io.BytesIO(image_data), 
                            mimetype='image/jpeg',
                            resumable=True)
    
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, webViewLink'
    ).execute()
    
    # Make the file publicly viewable
    drive_service.permissions().create(
        fileId=file['id'],
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()
    
    return file['webViewLink']

async def store_to_google_sheet(chat_id, user_data, context):  # Make this async
    """Store data with proper naming and organization"""
    chat_name = await get_chat_name(chat_id, context)  # Add await
    user_name = get_user_name(user_data['user'])
    
    # Get or create the chat folder and sheet
    sheet_id = get_or_create_sheet(chat_id, chat_name)
    chat_folder_id = get_or_create_folder(ROOT_FOLDER_ID, chat_name)
    
    # Upload the image to the chat's folder
    image_data = user_data.get("image_meta", {}).get("photo_data")
    if image_data:
        timestamp = user_data.get("image_meta", {}).get("date", "")
        formatted_date = format_datetime_for_sheets(timestamp)
        filename = f"{user_name}_{formatted_date.replace('/', '-').replace(':', '-')}.jpg"
        image_url = upload_image_to_drive(image_data, filename, chat_folder_id)
    else:
        image_url = "No image"


    # Prepare the row data with formatted date
    row = [
        user_name,
        user_data.get("caption", ""),
        user_data.get("follow_up_text", ""),
        format_datetime_for_sheets(user_data.get("image_meta", {}).get("date", "")),
        image_url,
        f'=IMAGE("{image_url}")' if image_url != "No image" else "No image"
    ]

    try:
        # First, ensure we have headers
        headers = ["User", "Caption", "Follow-up Text", "Date", "Image URL", "Image"]
        
        # Check if sheet is empty (need to add headers)
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range='A1:Z1'
        ).execute()
        
        if 'values' not in result:
            # Add headers
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range='A1',
                valueInputOption="RAW",
                body={"values": [headers]}
            ).execute()
        
        # Append the new row
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range='A1',
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]}
        ).execute()
        
        # Auto-resize columns and format date column
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": 0,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": len(headers)
                            }
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": 0,
                                "startRowIndex": 1,
                                "startColumnIndex": 3,  # Date column (D)
                                "endColumnIndex": 4
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "DATE_TIME",
                                        "pattern": "mm/dd/yyyy hh:mm:ss"
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat"
                        }
                    }
                ]
            }
        ).execute()
        
    except HttpError as e:
        print(f"Failed to append row: {e}")