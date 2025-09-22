# app/api/routes/tasks.py
import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, status, Header
from typing import Dict, Any, Optional
from pydantic import BaseModel
from supabase import Client
from google.oauth2 import id_token
from google.auth.transport import requests

from app.db.supabase import get_supabase
from app.services.chapter_service import ChapterService
from app.services.cloud_tasks_service import CloudTasksService
from app.core.logging import get_logger
from app.core.config import settings

log = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Pydantic models for request validation
class SingleChapterGenerationTaskPayload(BaseModel):
    order_id: str
    book_id: str
    user_id: str
    chapter_idx: int
    module_index: int
    topic_index: int
    chapter_title: str
    retry_count: Optional[int] = 0

# --- Dependencies ---
def get_chapter_service(supabase: Client = Depends(get_supabase)) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    from openai import OpenAI
    return ChapterService(supabase=supabase, openai_client=OpenAI())

def get_cloud_tasks_service() -> CloudTasksService:
    """Dependency to provide a CloudTasksService instance."""
    return CloudTasksService()

def verify_single_chapter_authorization(authorization: str = Header(None)) -> bool:
    """
    Verify that the request is authorized by validating the Google-signed OIDC token
    specifically for single chapter tasks.
    """
    if not authorization:
        log.warning("Single chapter task endpoint called without authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )
    
    # Extract token from "Bearer <token>" format
    try:
        scheme, token = authorization.split(" ")
        if scheme.lower() != "bearer":
            raise ValueError("Invalid scheme")
    except ValueError:
        log.warning("Single chapter task endpoint called with invalid authorization format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )

    try:
        # Define the expected audience for single chapter tasks only
        expected_audience = f"{settings.BACKEND_URL}/tasks/generate-single-chapter"
        
        log.debug(f"Attempting to verify single chapter token with audience: {expected_audience}")
        id_info = id_token.verify_oauth2_token(
            token, requests.Request(), audience=expected_audience
        )
        log.info(f"Single chapter token verified successfully with audience: {expected_audience}")
        
        # Optional: You can log the verified email for audit purposes
        log.info(f"Single chapter task token verified for service account: {id_info.get('email')}")

    except ValueError as e:
        # This will catch invalid tokens, expired tokens, or audience mismatches
        log.error(f"Invalid single chapter OIDC token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token"
        )
    
    return True

# --- Helper Functions ---

async def handle_non_subscription_completion(
    order_id: str,
    book_id: str, 
    user_id: str,
    supabase: Client
):
    """
    Handles PDF generation and email sending for non-subscription books.
    This is the same function from cart.py but moved here for Cloud Tasks.
    """
    try:
        # Check if this book is a subscription in the order_items table
        try:
            order_item_response = supabase.table("order_items").select("subscription").eq("order_id", order_id).eq("book_id", book_id).execute()
            
            if not order_item_response.data or len(order_item_response.data) == 0:
                log.warning(f"[Cloud Task] No order item found for book_id: {book_id} in order: {order_id}")
                return
                
            is_subscription = order_item_response.data[0].get("subscription", True)
            log.info(f"[Cloud Task] Order item subscription status for book_id: {book_id} is: {is_subscription}")
            
        except Exception as e:
            log.error(f"[Cloud Task] Error fetching order item for book_id: {book_id}, order_id: {order_id}. Error: {e}", exc_info=True)
            return
        
        if is_subscription:
            log.info(f"[Cloud Task] Book_id: {book_id} is a subscription, skipping PDF generation and email.")
            return
            
        log.info(f"[Cloud Task] Book_id: {book_id} is not a subscription, proceeding with PDF generation and email.")
        
        # Get user email from profiles table with fallback to auth.users
        user_email = None
        
        try:
            # First try to get email from profiles table
            profile_response = supabase.table("profiles").select("email").eq("user_id", user_id).execute()
            
            if profile_response.data and len(profile_response.data) > 0 and profile_response.data[0].get("email"):
                user_email = profile_response.data[0]["email"]
                log.info(f"[Cloud Task] Found email in profiles table for user_id: {user_id}")
            else:
                log.warning(f"[Cloud Task] No email found in profiles table for user_id: {user_id}, trying auth.users")
                
                # Fallback to auth.users table
                auth_response = supabase.auth.admin.get_user_by_id(user_id)
                if auth_response and hasattr(auth_response, 'user') and auth_response.user and auth_response.user.email:
                    user_email = auth_response.user.email
                    log.info(f"[Cloud Task] Found email in auth.users table for user_id: {user_id}")
                else:
                    log.error(f"[Cloud Task] No email found in either profiles or auth.users for user_id: {user_id}")
                    return
                    
        except Exception as e:
            log.error(f"[Cloud Task] Error fetching user email for user_id: {user_id}. Error: {e}", exc_info=True)
            return
            
        if not user_email:
            log.error(f"[Cloud Task] No email found for user_id: {user_id}")
            return
        
        # Get book title for PDF generation
        book_title = "SARAS eBook"  # Default title
        try:
            book_response = supabase.table("books").select("generated_title").eq("id", book_id).execute()
            if book_response.data and len(book_response.data) > 0 and book_response.data[0].get("generated_title"):
                book_title = book_response.data[0]["generated_title"]
                log.info(f"[Cloud Task] Found book title: {book_title} for book_id: {book_id}")
            else:
                log.warning(f"[Cloud Task] No book title found for book_id: {book_id}, using default")
        except Exception as e:
            log.error(f"[Cloud Task] Error fetching book title for book_id: {book_id}. Error: {e}", exc_info=True)
            # Continue with default title
        
        # Generate PDF using the PDF service directly
        try:
            from app.services.pdf_service import PDFService
            pdf_service = PDFService(supabase)
            pdf_result = pdf_service.create_book_pdf(
                book_id=book_id,
                book_title=book_title,
                subscription=False
            )
            log.info(f"[Cloud Task] PDF generated successfully for book_id: {book_id}. PDF URL: {pdf_result.get('storage_url')}")
        except Exception as e:
            log.error(f"[Cloud Task] PDF generation failed for book_id: {book_id}. Error: {e}", exc_info=True)
            return
        
        # Send email with PDF attachment using the email service directly
        try:
            from app.services.email_service import EmailService
            email_service = EmailService()
            email_result = email_service.send_email_with_attachment(
                recipient_email=user_email,
                subject=f"Your Book: {book_title}",
                html_content=f"""
                <html>
                <body>
                    <h2>Your Book is Ready!</h2>
                    <p>Hello!</p>
                    <p>Your book "{book_title}" has been generated and is ready for download. Please find the PDF attached to this email.</p>
                    <p>Thank you for using our service!</p>
                    <br>
                    <p>Best regards,<br>The Team</p>
                </body>
                </html>
                """,
                attachment_path=pdf_result.get("storage_url")
            )
            log.info(f"[Cloud Task] Email sent successfully to {user_email} for book_id: {book_id}")
        except Exception as e:
            log.error(f"[Cloud Task] Email sending failed for book_id: {book_id}. Error: {e}", exc_info=True)
            return
        
    except Exception as e:
        log.error(f"[Cloud Task] Error in handle_non_subscription_completion for book_id: {book_id}. Error: {e}", exc_info=True)
        raise

async def process_single_chapter_generation_task(
    payload: SingleChapterGenerationTaskPayload,
    chapter_service: ChapterService,
    supabase: Client
):
    """
    Process a single chapter generation task.
    """
    order_id = payload.order_id
    book_id = payload.book_id
    user_id = payload.user_id
    chapter_idx = payload.chapter_idx
    module_index = payload.module_index
    topic_index = payload.topic_index
    chapter_title = payload.chapter_title
    retry_count = payload.retry_count or 0

    try:
        log.info(f"[Cloud Task] Generating single chapter for book_id: {book_id}, chapter_idx: {chapter_idx}, order_id: {order_id}")
        
        # Generate the individual chapter
        chapter_result = chapter_service.generate_chapter(
            user_id=user_id,
            wish_id=None,
            book_id=book_id,
            idx=chapter_idx,
            module_index=module_index,
            topic_index=topic_index
        )
        
        log.info(f"[Cloud Task] Successfully generated chapter {chapter_idx} for book_id: {book_id}")
        
        # Check if this was the last chapter for this book and handle completion
        await check_and_handle_book_completion(order_id, book_id, user_id, supabase)
        
        return chapter_result
        
    except Exception as e:
        log.error(f"[Cloud Task] Failed to generate chapter {chapter_idx} for book_id: {book_id} in order {order_id}. Error: {e}", exc_info=True)
        
        # Update the specific chapter status to failed if possible
        try:
            # Try to find the chapter record and mark it as failed
            chapter_response = supabase.table("chapters").select("id").eq("book_id", book_id).eq("idx", chapter_idx).execute()
            if chapter_response.data:
                chapter_id = chapter_response.data[0]["id"]
                supabase.table("chapters").update({"status": "failed"}).eq("id", chapter_id).execute()
                log.info(f"[Cloud Task] Marked chapter {chapter_idx} as failed for book_id: {book_id}")
        except Exception as update_error:
            log.error(f"[Cloud Task] Failed to mark chapter {chapter_idx} as failed for book_id: {book_id}. Error: {update_error}", exc_info=True)
        
        raise

async def verify_all_chapters_uploaded(book_id: str, expected_chapter_count: int, supabase: Client) -> bool:
    """
    Verify that all expected chapter markdown files are actually uploaded to storage.
    """
    try:
        log.info(f"[Cloud Task] Verifying all {expected_chapter_count} chapters are uploaded for book_id: {book_id}")
        
        # Check each expected chapter file in storage
        chapters_bucket = "chapters"
        uploaded_count = 0
        missing_chapters = []
        
        for chapter_idx in range(1, expected_chapter_count + 1):
            chapter_path = f"books/{book_id}/chapters/{chapter_idx}.md"
            try:
                # Try to download just the metadata (head request equivalent)
                supabase.storage.from_(chapters_bucket).download(chapter_path)
                uploaded_count += 1
                log.debug(f"[Cloud Task] Chapter {chapter_idx} confirmed uploaded for book_id: {book_id}")
            except Exception as e:
                missing_chapters.append(chapter_idx)
                log.debug(f"[Cloud Task] Chapter {chapter_idx} not yet uploaded for book_id: {book_id}: {e}")
        
        all_uploaded = uploaded_count == expected_chapter_count
        
        if all_uploaded:
            log.info(f"[Cloud Task] All {expected_chapter_count} chapters confirmed uploaded for book_id: {book_id}")
        else:
            log.info(f"[Cloud Task] Book {book_id}: {uploaded_count}/{expected_chapter_count} chapters uploaded. Missing: {missing_chapters}")
        
        return all_uploaded
        
    except Exception as e:
        log.error(f"[Cloud Task] Error verifying chapter uploads for book_id: {book_id}. Error: {e}", exc_info=True)
        return False

async def check_and_handle_book_completion(order_id: str, book_id: str, user_id: str, supabase: Client):
    """
    Check if all chapters for a book are completed and handle book completion logic.
    Now uses robust storage verification before triggering PDF generation.
    """
    try:
        # Get total expected chapters for this book by loading the roadmap
        book_response = supabase.table("books").select("content_url").eq("id", book_id).single().execute()
        if not book_response.data:
            log.error(f"[Cloud Task] Book not found for book_id: {book_id}")
            return
        
        # Download and parse the roadmap to count expected chapters
        roadmap_path = book_response.data["content_url"].replace("books/", "", 1)
        log.debug(f"[Cloud Task] Loading roadmap for book_id: {book_id} from path: {roadmap_path}")
        try:
            roadmap_raw = supabase.storage.from_("books").download(roadmap_path)
            roadmap = json.loads(roadmap_raw.decode("utf-8"))
            
            total_expected_chapters = 0
            for module in roadmap.get("modules", []):
                total_expected_chapters += len(module.get("topics", []))
                
        except Exception as e:
            log.error(f"[Cloud Task] Failed to load roadmap for book_id: {book_id}. Error: {e}", exc_info=True)
            return
        
        # Get chapter database records for status tracking
        chapters_response = supabase.table("chapters").select("id, idx, status, content_path").eq("book_id", book_id).execute()
        if not chapters_response.data:
            log.warning(f"[Cloud Task] No chapters found for book_id: {book_id}")
            return
        
        # Count chapters by status
        completed_db_records = []
        failed_chapters = []
        pending_chapters = []
        
        for ch in chapters_response.data:
            status = ch.get("status")
            content_path = ch.get("content_path")
            
            if status == "failed":
                failed_chapters.append(ch)
            elif content_path:  # Has content_path in database
                completed_db_records.append(ch)
            else:  # Database record exists but no content_path yet
                pending_chapters.append(ch)
        
        log.info(f"[Cloud Task] Book {book_id} database status: {len(completed_db_records)}/{total_expected_chapters} chapters with content_path, {len(failed_chapters)} failed, {len(pending_chapters)} pending")
        
        # Only proceed if we have the expected number of database records with content_path
        if len(completed_db_records) + len(failed_chapters) < total_expected_chapters:
            log.info(f"[Cloud Task] Book {book_id} still has chapters being processed. Waiting for completion.")
            return
        
        # Now verify that all expected chapter files are actually uploaded to storage
        if len(failed_chapters) == 0:  # Only check storage if no failed chapters
            all_files_uploaded = await verify_all_chapters_uploaded(book_id, total_expected_chapters, supabase)
            
            if not all_files_uploaded:
                log.info(f"[Cloud Task] Book {book_id} database shows completion but not all files are uploaded yet. Waiting...")
                return
            
            # Add a small delay to ensure all file uploads are fully committed
            await asyncio.sleep(2)  # 2-second buffer to ensure storage consistency
            log.debug(f"[Cloud Task] Waited 2 seconds for storage consistency before PDF generation for book_id: {book_id}")
        
        # All chapters are truly completed (or failed), determine final book status
        book_status = "completed" if len(failed_chapters) == 0 else "failed"
        
        try:
            # Update book status
            supabase.table("books").update({"status": book_status}).eq("id", book_id).execute()
            log.info(f"[Cloud Task] Updated book_id: {book_id} status to '{book_status}' after verifying all files uploaded")
            
            # If book completed successfully, handle PDF generation and email for non-subscription books
            if book_status == "completed":
                log.info(f"[Cloud Task] All chapters verified uploaded for book_id: {book_id}. Triggering PDF generation.")
                await handle_non_subscription_completion(order_id, book_id, user_id, supabase)
            
            # Check if all books in the order are completed
            await check_and_handle_order_completion(order_id, supabase)
            
        except Exception as e:
            log.error(f"[Cloud Task] Failed to update book status for book_id: {book_id}. Error: {e}", exc_info=True)
        
    except Exception as e:
        log.error(f"[Cloud Task] Error in check_and_handle_book_completion for book_id: {book_id}. Error: {e}", exc_info=True)

async def check_and_handle_order_completion(order_id: str, supabase: Client):
    """
    Check if all books in an order are completed and update order status.
    """
    try:
        # Get all books for this order
        order_response = supabase.table("orders").select("order_items(book_id)").eq("id", order_id).single().execute()
        if not order_response.data or not order_response.data.get("order_items"):
            log.warning(f"[Cloud Task] No order items found for order_id: {order_id}")
            return
        
        book_ids = [item["book_id"] for item in order_response.data["order_items"]]
        
        # Check status of all books
        books_response = supabase.table("books").select("id, status").in_("id", book_ids).execute()
        if not books_response.data:
            log.warning(f"[Cloud Task] No books found for order_id: {order_id}")
            return
        
        book_statuses = {book["id"]: book.get("status", "pending") for book in books_response.data}
        
        # Check if all books are either completed or failed
        pending_books = [book_id for book_id, status in book_statuses.items() if status not in ["completed", "failed"]]
        
        if not pending_books:
            # All books are processed, determine final order status
            failed_books = [book_id for book_id, status in book_statuses.items() if status == "failed"]
            order_status = "failed" if failed_books else "completed"
            
            try:
                supabase.table("orders").update({"status": order_status}).eq("id", order_id).execute()
                log.info(f"[Cloud Task] Updated order_id: {order_id} final status to '{order_status}'")
            except Exception as e:
                log.error(f"[Cloud Task] Failed to update final order status for order_id: {order_id}. Error: {e}", exc_info=True)
        else:
            log.info(f"[Cloud Task] Order {order_id} still has {len(pending_books)} pending books: {pending_books}")
            
    except Exception as e:
        log.error(f"[Cloud Task] Error in check_and_handle_order_completion for order_id: {order_id}. Error: {e}", exc_info=True)

# --- API Routes ---

@router.post("/generate-single-chapter", summary="Cloud Tasks handler for single chapter generation")
async def handle_single_chapter_generation_task(
    payload: SingleChapterGenerationTaskPayload,
    _: bool = Depends(verify_single_chapter_authorization),
    chapter_service: ChapterService = Depends(get_chapter_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle single chapter generation tasks from Google Cloud Tasks.
    This endpoint is called by Cloud Tasks to process individual chapter generation jobs.
    """
    order_id = payload.order_id
    book_id = payload.book_id
    chapter_idx = payload.chapter_idx
    retry_count = payload.retry_count or 0
    max_retries = 3
    
    log.info(f"[Single Chapter Task Handler] Received single chapter generation task for order_id: {order_id}, book_id: {book_id}, chapter_idx: {chapter_idx} (retry: {retry_count})")
    
    try:
        # Process the single chapter generation
        await process_single_chapter_generation_task(
            payload=payload,
            chapter_service=chapter_service,
            supabase=supabase
        )
        
        log.info(f"[Single Chapter Task Handler] Successfully completed chapter generation for order_id: {order_id}, book_id: {book_id}, chapter_idx: {chapter_idx}")
        return {
            "status": "success",
            "message": f"Single chapter generation completed for order {order_id}, book {book_id}, chapter {chapter_idx}",
            "order_id": order_id,
            "book_id": book_id,
            "chapter_idx": chapter_idx,
            "retry_count": retry_count
        }
        
    except Exception as e:
        log.error(f"[Single Chapter Task Handler] Single chapter generation failed for order_id: {order_id}, book_id: {book_id}, chapter_idx: {chapter_idx} (retry: {retry_count}). Error: {e}", exc_info=True)
        
        # If we haven't exceeded max retries, create a retry task
        if retry_count < max_retries:
            try:
                # Create a retry task for this specific chapter
                retry_task_name = cloud_tasks_service.create_single_chapter_generation_task(
                    order_id=order_id,
                    book_id=book_id,
                    user_id=payload.user_id,
                    chapter_idx=chapter_idx,
                    module_index=payload.module_index,
                    topic_index=payload.topic_index,
                    chapter_title=payload.chapter_title,
                    delay_seconds=300 * (retry_count + 1)  # Exponential backoff: 5, 10, 15 minutes
                )
                log.info(f"[Single Chapter Task Handler] Created retry task {retry_task_name} for chapter {chapter_idx}")
                
                return {
                    "status": "retry_scheduled",
                    "message": f"Chapter task failed, retry scheduled (attempt {retry_count + 1}/{max_retries})",
                    "order_id": order_id,
                    "book_id": book_id,
                    "chapter_idx": chapter_idx,
                    "retry_count": retry_count + 1,
                    "retry_task_name": retry_task_name
                }
            except Exception as retry_error:
                log.error(f"[Single Chapter Task Handler] Failed to create retry task for chapter {chapter_idx}. Error: {retry_error}", exc_info=True)
        
        # Mark chapter as failed if we've exceeded retries or can't create retry task
        try:
            chapter_response = supabase.table("chapters").select("id").eq("book_id", book_id).eq("idx", chapter_idx).execute()
            if chapter_response.data:
                chapter_id = chapter_response.data[0]["id"]
                supabase.table("chapters").update({"status": "failed"}).eq("id", chapter_id).execute()
                log.info(f"[Single Chapter Task Handler] Marked chapter {chapter_idx} as failed after {retry_count + 1} attempts")
        except Exception as update_error:
            log.error(f"[Single Chapter Task Handler] Failed to mark chapter {chapter_idx} as failed. Error: {update_error}", exc_info=True)
        
        # Return error response
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Single chapter generation failed for order {order_id}, book {book_id}, chapter {chapter_idx} after {retry_count + 1} attempts"
        )

@router.get("/queue-info", summary="Get Cloud Tasks queue information")
def get_queue_info(
    _: bool = Depends(verify_single_chapter_authorization),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service)
):
    """
    Get information about the Cloud Tasks queue.
    This endpoint can be used for monitoring and debugging.
    """
    try:
        queue_info = cloud_tasks_service.get_queue_info()
        log.info(f"Queue info requested: {queue_info}")
        return queue_info
    except Exception as e:
        log.error(f"Failed to get queue info: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get queue information"
        )
