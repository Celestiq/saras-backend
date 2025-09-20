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

    def send_confirmation_email(self, order_id: str) -> Dict[str, Any]:
        """
        Sends a confirmation email after successful payment using the confirmation.html template.
        Fetches all required data from the database using the order_id.
        
        Args:
            order_id: The order ID to send confirmation email for
            
        Returns:
            Dict containing the response from Zoho API
        """
        log.info(f"Preparing to send confirmation email for order {order_id}")
        
        try:
            # Import here to avoid circular imports
            from app.db.supabase import get_supabase
            from datetime import datetime
            
            supabase = get_supabase()
            
            # Fetch order details with order items, books, and user profile
            order_query = supabase.table("orders").select("""
                *,
                order_items (
                    *,
                    books (
                        id,
                        generated_title
                    )
                ),
                profiles!orders_user_id_fkey (
                    user_id,
                    full_name,
                    email
                )
            """).eq("id", order_id).single().execute()
            
            if not order_query.data:
                log.error(f"Order not found: {order_id}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Order not found"
                )
            
            order_data = order_query.data
            user_profile = order_data.get("profiles")
            order_items = order_data.get("order_items", [])
            
            if not user_profile:
                log.error(f"User profile not found for order: {order_id}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User profile not found"
                )
            
            if not user_profile.get("email"):
                log.error(f"User email not found for order: {order_id}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User email not found"
                )
            
            # Extract data
            user_email = user_profile.get("email")
            user_name = user_profile.get("full_name", "User")
            total_amount = order_data.get("total", 0.0)
            order_date = datetime.fromisoformat(order_data.get("created_at").replace('Z', '+00:00')).strftime("%B %d, %Y")
            
            # Fetch the confirmation.html template from Supabase storage
            try:
                template_data = supabase.storage.from_("html").download("utility/confirmation.html")
                html_template = template_data.decode('utf-8')
                log.info("Successfully fetched confirmation email template from Supabase storage")
            except Exception as template_error:
                log.error(f"Failed to fetch email template from Supabase storage: {template_error}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to fetch email template from storage"
                )
            
            # Replace basic placeholders
            html_content = html_template.replace("[User's Name]", user_name)
            html_content = html_content.replace("[Order ID]", f"#{order_id}")
            html_content = html_content.replace("[Date]", order_date)
            html_content = html_content.replace("$[Total Amount]", f"${total_amount:.2f}")
            
            # Handle order items - build the items section
            if order_items:
                items_html = ""
                for item in order_items:
                    book_title = "Your Book"
                    if item.get("books"):
                        book_title = item["books"].get("generated_title", "Your Book")
                    
                    order_type = "Newsletter Subscription" if item.get("subscription", True) else "eBook"
                    item_price = item.get("unit_price", 0.0)
                    quantity = item.get("quantity", 1)
                    
                    # Add each item
                    item_html = f"""
                                                    <tr>
                                                        <td style="padding-top: 15px;">
                                                            <p style="font-family: 'Merriweather', serif; font-size: 16px; color: #3a3a3a; margin: 0 0 5px 0;">
                                                                <strong>{book_title}</strong>
                                                            </p>
                                                            <p style="font-family: 'Merriweather', serif; font-size: 14px; color: #6a6a6a; margin: 0;">
                                                                <em>{order_type}</em> • Qty: {quantity} • ${item_price:.2f}
                                                            </p>
                                                        </td>
                                                    </tr>"""
                    items_html += item_html
                
                # Replace the single item placeholder with multiple items
                single_item_pattern = '''                                            <tr>
                                                <td style="padding-top: 15px;">
                                                    <p style="font-family: 'Merriweather', serif; font-size: 16px; color: #3a3a3a; margin: 0 0 5px 0;">
                                                        <strong>[Book Title]</strong>
                                                    </p>
                                                    <p style="font-family: 'Merriweather', serif; font-size: 14px; color: #6a6a6a; margin: 0;">
                                                        <em>[Newsletter Subscription / Full eBook]</em>
                                                    </p>
                                                </td>
                                            </tr>'''
                
                html_content = html_content.replace(single_item_pattern, items_html)
            else:
                # Fallback for no items
                html_content = html_content.replace("[Book Title]", "Your Book")
                html_content = html_content.replace("[Newsletter Subscription / Full eBook]", "Full eBook")
            
            # Set manage orders URL
            manage_orders_url = f"{settings.FRONTEND_URL}/manage-orders" if hasattr(settings, 'FRONTEND_URL') else "#"
            html_content = html_content.replace("[Link to Manage Orders Page]", manage_orders_url)
            
            subject = f"Your Saras Order #{order_id} is Confirmed!"
            
            # Send the email using the simple email method
            return self.send_simple_email(
                recipient_email=user_email,
                subject=subject,
                html_content=html_content
            )
            
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Error sending confirmation email for order {order_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send confirmation email: {str(e)}"
            )

if __name__ == "__main__":
    # Simple test to send an email (replace with actual values)
    email_service = EmailService()
    access_token = email_service.get_access_token()
    print(f"Access Token: {access_token}")
    account_id = email_service.get_account_id(access_token)
    print(f"Account ID: {account_id}")