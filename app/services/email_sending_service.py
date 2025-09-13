# app/services/email_sending_service.py
from __future__ import annotations
from typing import List, Dict, Any
from supabase import Client
from app.core.logging import get_logger
from app.services.email_service import EmailService
from app.services.html_conversion_service import HTMLConversionService

from app.db.supabase import get_supabase

log = get_logger(__name__)

class DailyEmailService:
    """
    Handles the daily workflow of sending chapter emails to subscribers.
    This service is designed to be run as a scheduled task.
    """

    def __init__(self, supabase: Client):
        """Initializes the service with a Supabase client and other required services."""
        self.sb = supabase
        self.email_service = EmailService()
        self.html_service = HTMLConversionService(supabase)
        log.info("DailyEmailService initialized.")

    def _get_daily_subscriptions(self) -> List[Dict[str, Any]]:
        """
        Fetches all subscriptions that are due for an email today.

        This query uses the "stateless, calculated" approach to determine which
        chapter to send based on the subscription's start date.

        Returns:
            A list of records, each containing user, book, and chapter details.
        """
        log.info("Fetching today's due subscriptions...")
        try:
            # Note: Supabase Python client doesn't support DATE_PART directly.
            # We create a PostgreSQL function to handle the logic.
            # See documentation below for the function definition.
            rpc_params = {}
            response = self.sb.rpc('get_daily_email_batch', rpc_params).execute()

            if response.data:
                log.info(f"Found {len(response.data)} emails to send today.")
                return response.data
            else:
                log.info("No emails to send today.")
                return []
        except Exception as e:
            log.error(f"Error fetching daily subscriptions via RPC: {e}", exc_info=True)
            return []

    def _prepare_and_send_email(self, subscription_data: Dict[str, Any]) -> Dict[str, Any]:
        """

        Prepares and sends a single chapter email.

        Args:
            subscription_data: A dictionary containing all necessary data for one email.

        Returns:
            A dictionary with the status of the email sending operation.
        """
        recipient_email = subscription_data.get("email")
        book_title = subscription_data.get("book_title")
        chapter_title = subscription_data.get("chapter_title")
        chapter_idx = subscription_data.get("chapter_idx")
        content_path = subscription_data.get("content_path") 
        chapter_id = subscription_data.get("chapter_id")

        log.info(f"Processing email for {recipient_email} - Book: '{book_title}', Chapter: {chapter_idx}")

        if not all([recipient_email, book_title, chapter_title, content_path, chapter_id]):
            log.error(f"Missing critical data in subscription record: {subscription_data}")
            return {"status": "failed", "reason": "Missing data"}

        try:
            html_response = self.html_service.convert_chapter_to_html(chapter_id)
            html_path = html_response.get("html_path")
            if not html_path:
                log.error(f"HTML conversion failed for chapter_id {chapter_id}")
                raise ValueError("HTML conversion failed, no html_path returned.")
            
            bucket, *path_parts = html_path.split("/")
            path = "/".join(path_parts)
            
            log.info(f"Downloading html from storage at {bucket}/{path}")
            raw_content = self.sb.storage.from_(bucket).download(path)
            html_body = raw_content.decode('utf-8')

            # 2. Send the email
            subject = f"{book_title} - Day {chapter_idx}: {chapter_title}"
            self.email_service.send_simple_email(
                recipient_email=recipient_email,
                subject=subject,
                html_content=html_body
            )

            log.info(f"Successfully sent Chapter {chapter_idx} of '{book_title}' to {recipient_email}")
            return {"status": "success", "recipient": recipient_email, "subject": subject}

        except Exception as e:
            log.error(f"Failed to send email for {recipient_email} (Chapter {chapter_idx}, Book '{book_title}'). Error: {e}", exc_info=True)
            return {"status": "failed", "recipient": recipient_email, "reason": str(e)}

    def run_daily_email_workflow(self) -> Dict[str, Any]:
        """
        Orchestrates the entire daily email sending process.
        This is the main entry point to be called by the scheduled task.
        """
        log.info("Starting daily email sending workflow...")
        
        subscriptions_to_process = self._get_daily_subscriptions()
        
        if not subscriptions_to_process:
            log.info("Daily email workflow finished. No subscriptions to process.")
            return {"status": "complete", "sent": 0, "failed": 0, "total": 0}

        success_count = 0
        failure_count = 0
        
        for subscription in subscriptions_to_process:
            result = self._prepare_and_send_email(subscription)
            if result["status"] == "success":
                success_count += 1
            else:
                failure_count += 1

        total_processed = len(subscriptions_to_process)
        log.info("Daily email sending workflow finished.")
        log.info(f"Summary: Total processed: {total_processed}, Successful: {success_count}, Failed: {failure_count}")

        return {
            "status": "complete",
            "sent": success_count,
            "failed": failure_count,
            "total": total_processed
        }
    
if __name__ == "__main__":
    daily_email_service = DailyEmailService(supabase=get_supabase())

    query_response = daily_email_service._get_daily_subscriptions()
    print(f"Query response: {query_response}")

    mail_response = daily_email_service.run_daily_email_workflow()
    print(f"Mail response: {mail_response}")


