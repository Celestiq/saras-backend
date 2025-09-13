# app/services/email_service.py
import requests
import base64
import json
import os
import tempfile
from typing import Optional, Dict, Any
from fastapi import HTTPException, status
from app.core.config import settings
from app.core.logging import get_logger
from app.db.supabase import get_supabase

log = get_logger(__name__)

class EmailService:
    """Handles email sending functionality using Zoho Mail API."""

    def __init__(self):
        self.client_id = settings.ZOHO_CLIENT_ID
        self.client_secret = settings.ZOHO_CLIENT_SECRET
        self.refresh_token = settings.ZOHO_REFRESH_TOKEN
        self.from_email = settings.ZOHO_FROM_EMAIL
        self.account_id = None

    def get_access_token(self) -> str:
        """Uses the refresh token to get a new access token."""
        log.info("Getting new access token from Zoho...")
        token_url = "https://accounts.zoho.in/oauth/v2/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token"
        }
        try:
            response = requests.post(token_url, data=payload)
            log.debug(f"Access Token Response: {response.text}")
            
            if response.status_code == 200:
                access_token = response.json().get("access_token")
                log.info("Successfully obtained new access token")
                return access_token
            else:
                log.error(f"Error getting access token: {response.text}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to obtain access token from Zoho"
                )
        except requests.RequestException as e:
            log.error(f"Network error while getting access token: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Network error while authenticating with Zoho"
            )

    def get_account_id(self, access_token: str) -> str:
        """Fetches the Zoho Account ID needed for sending emails."""
        log.info("Fetching Zoho Account ID...")
        url = "https://mail.zoho.in/api/accounts"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                account_id = data['data'][0]['accountId']
                log.info(f"Found Account ID: {account_id}")
                return account_id
            else:
                log.error(f"Error fetching account ID: {response.text}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to fetch Zoho account ID"
                )
        except requests.RequestException as e:
            log.error(f"Network error while fetching account ID: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Network error while fetching account information"
            )

    def _download_from_supabase(self, storage_url: str) -> str:
        """Downloads a file from Supabase storage and returns the local file path."""
        try:
            # Parse the storage URL to get bucket and path
            if "/" not in storage_url:
                raise ValueError(f"Invalid storage URL format: {storage_url}")
            
            bucket, *path_parts = storage_url.split("/", 1)
            if len(path_parts) == 0:
                raise ValueError(f"Invalid storage URL format: {storage_url}")
            
            path = path_parts[0]
            log.info(f"Downloading file from Supabase storage: {bucket}/{path}")
            
            # Get Supabase client
            supabase = get_supabase()
            
            # Download the file
            file_data = supabase.storage.from_(bucket).download(path)
            
            # Create a temporary file
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(path)[1])
            temp_file.write(file_data)
            temp_file.close()
            
            log.info(f"Downloaded file to temporary location: {temp_file.name}")
            return temp_file.name
            
        except Exception as e:
            log.error(f"Error downloading file from Supabase storage: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to download file from storage: {e}"
            )

    def upload_attachment(self, access_token: str, account_id: str, file_path: str) -> Dict[str, str]:
        """Uploads an attachment and returns the attachment details."""
        # Check if it's a Supabase storage URL
        is_supabase_url = file_path.startswith(('ebooks/', 'books/', 'chapters/')) or 'supabase' in file_path.lower()
        
        local_file_path = file_path
        temp_file_created = False
        
        try:
            if is_supabase_url:
                log.info(f"Detected Supabase storage URL, downloading file: {file_path}")
                local_file_path = self._download_from_supabase(file_path)
                temp_file_created = True
            
            file_name = os.path.basename(local_file_path)
            upload_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages/attachments?uploadType=multipart&isInline=false"
            
            headers_upload = {
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Accept": "application/json"
            }

            with open(local_file_path, 'rb') as file_data:
                files = [('attach', (file_name, file_data))]
                response_upload = requests.post(upload_url, headers=headers_upload, files=files)
                
        except FileNotFoundError:
            log.error(f"Attachment file not found: {local_file_path}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Attachment file not found: {local_file_path}"
            )
        except Exception as e:
            log.error(f"Error opening attachment file: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error processing attachment file"
            )
        finally:
            # Clean up temporary file if we created one
            if temp_file_created and os.path.exists(local_file_path):
                try:
                    os.unlink(local_file_path)
                    log.info(f"Cleaned up temporary file: {local_file_path}")
                except Exception as e:
                    log.warning(f"Failed to clean up temporary file {local_file_path}: {e}")

        if response_upload.status_code != 200:
            log.error(f"Failed to upload attachment. Status: {response_upload.status_code}, Response: {response_upload.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload attachment to Zoho"
            )

        upload_data = response_upload.json()
        return {
            "attachment_path": upload_data['data'][0]['attachmentPath'],
            "store_name": upload_data['data'][0]['storeName'],
            "attachment_name": upload_data['data'][0]['attachmentName']
        }

    def send_email_with_attachment(
        self, 
        recipient_email: str, 
        subject: str, 
        html_content: str, 
        attachment_path: str
    ) -> Dict[str, Any]:
        """
        Sends an email with an attachment using Zoho Mail API.
        
        Args:
            recipient_email: Email address of the recipient
            subject: Email subject
            html_content: HTML content of the email
            attachment_path: Path to the file to attach
            
        Returns:
            Dict containing the response from Zoho API
        """
        log.info(f"Preparing to send email to {recipient_email}")
        
        try:
            # Get access token and account ID
            access_token = self.get_access_token()
            account_id = self.get_account_id(access_token)
            
            # Upload attachment
            attachment_details = self.upload_attachment(access_token, account_id, attachment_path)
            
            # Prepare email payload
            email_payload = {
                "fromAddress": self.from_email,
                "toAddress": recipient_email,
                "subject": subject,
                "content": html_content,
                "attachments": [{
                    "attachmentPath": attachment_details["attachment_path"],
                    "storeName": attachment_details["store_name"],
                    "attachmentName": attachment_details["attachment_name"]
                }]
            }

            # Send email
            send_mail_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages"
            headers_send = {"Authorization": f"Zoho-oauthtoken {access_token}"}
            
            response_send = requests.post(send_mail_url, headers=headers_send, data=json.dumps(email_payload))

            if response_send.status_code == 200:
                log.info("Email sent successfully!")
                return {
                    "status": "success",
                    "message": "Email sent successfully",
                    "recipient": recipient_email,
                    "subject": subject
                }
            else:
                log.error(f"Failed to send email. Status: {response_send.status_code}, Response: {response_send.text}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to send email: {response_send.text}"
                )

        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Unexpected error while sending email: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error while sending email: {str(e)}"
            )

    def send_simple_email(
        self, 
        recipient_email: str, 
        subject: str, 
        html_content: str
    ) -> Dict[str, Any]:
        """
        Sends a simple email without attachments using Zoho Mail API.
        
        Args:
            recipient_email: Email address of the recipient
            subject: Email subject
            html_content: HTML content of the email
            
        Returns:
            Dict containing the response from Zoho API
        """
        log.info(f"Preparing to send simple email to {recipient_email}")
        
        try:
            # Get access token and account ID
            access_token = self.get_access_token()
            account_id = self.get_account_id(access_token)
            
            # Prepare email payload
            email_payload = {
                "fromAddress": self.from_email,
                "toAddress": recipient_email,
                "subject": subject,
                "content": html_content
            }

            # Send email
            send_mail_url = f"https://mail.zoho.in/api/accounts/{account_id}/messages"
            headers_send = {"Authorization": f"Zoho-oauthtoken {access_token}"}
            
            response_send = requests.post(send_mail_url, headers=headers_send, data=json.dumps(email_payload))

            if response_send.status_code == 200:
                log.info("Email sent successfully!")
                return {
                    "status": "success",
                    "message": "Email sent successfully",
                    "recipient": recipient_email,
                    "subject": subject
                }
            else:
                log.error(f"Failed to send email. Status: {response_send.status_code}, Response: {response_send.text}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to send email: {response_send.text}"
                )

        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Unexpected error while sending email: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error while sending email: {str(e)}"
            )

if __name__ == "__main__":
    # Simple test to send an email (replace with actual values)
    email_service = EmailService()
    access_token = email_service.get_access_token()
    print(f"Access Token: {access_token}")
    account_id = email_service.get_account_id(access_token)
    print(f"Account ID: {account_id}")