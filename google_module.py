# google_module.py (updated)
import os
import io
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image

# Load .env variables
load_dotenv()

SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
ROOT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")

print("DEBUG: SERVICE_ACCOUNT_FILE =", SERVICE_ACCOUNT_FILE)
print("DEBUG: ROOT_FOLDER_ID =", ROOT_FOLDER_ID)

if not SERVICE_ACCOUNT_FILE:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_FILE in environment.")

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

def upload_image_to_drive(image_data, filename, folder_id):
    """Upload image to Google Drive and return shareable link"""
    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }
    
    # Convert bytearray to file-like object
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

def store_to_google_sheet(chat_id, user_data):
    sheet_id = get_or_create_sheet_for_chat(chat_id)
    
    # Upload the image to Drive first
    image_data = user_data.get("image_meta", {}).get("photo_data")
    if image_data:
        filename = f"{user_data.get('username', 'unknown')}_{user_data.get('image_meta', {}).get('file_unique_id', '')}.jpg"
        image_url = upload_image_to_drive(image_data, filename, ROOT_FOLDER_ID)
    else:
        image_url = "No image"

    # Prepare the row data with image formula
    row = [
        user_data.get("username"),
        user_data.get("image_file_id"),
        user_data.get("caption"),
        user_data.get("follow_up_text"),
        user_data.get("image_meta", {}).get("date"),
        user_data.get("image_meta", {}).get("file_unique_id"),
        user_data.get("image_meta", {}).get("message_id"),
        user_data.get("text_timestamp"),
        image_url,  # Direct link to image
        f'=IMAGE("{image_url}")' if image_url != "No image" else "No image"  # This will display the image in the cell
    ]

    try:
        # First, ensure we have headers
        headers = [
            "Username", "File ID", "Caption", "Follow-up Text", 
            "Date", "Unique ID", "Message ID", "Text Timestamp",
            "Image URL", "Image"
        ]
        
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
            valueInputOption="USER_ENTERED",  # Changed to USER_ENTERED to process formulas
            insertDataOption="INSERT_ROWS",
            body={"values": [row]}
        ).execute()
        
        # Auto-resize columns to fit images
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [{
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": 0,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": len(headers)
                        }
                    }
                }]
            }
        ).execute()
        
    except HttpError as e:
        print(f"Failed to append row: {e}")