# app/api/routes/tasks.py
from fastapi import APIRouter, Depends, HTTPException, status, Header
from typing import Dict, Any, Optional
from pydantic import BaseModel
from supabase import Client

from app.db.supabase import get_supabase
from app.services.chapter_service import ChapterService
from app.services.cloud_tasks_service import CloudTasksService
from app.core.logging import get_logger
from app.core.config import settings

log = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Pydantic models for request validation
class ChapterGenerationTaskPayload(BaseModel):
    order_id: str
    retry_count: Optional[int] = 0

# --- Dependencies ---
def get_chapter_service(supabase: Client = Depends(get_supabase)) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    from openai import OpenAI
    return ChapterService(supabase=supabase, openai_client=OpenAI())

def get_cloud_tasks_service() -> CloudTasksService:
    """Dependency to provide a CloudTasksService instance."""
    return CloudTasksService()

def verify_task_authorization(authorization: str = Header(None)) -> bool:
    """Verify that the request is authorized (from Cloud Tasks)."""
    if not authorization:
        log.warning("Task endpoint called without authorization header")
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
        log.warning("Task endpoint called with invalid authorization format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )
    
    # Verify the token matches our job runner secret
    if token != settings.JOB_RUNNER_SECRET:
        log.warning("Task endpoint called with invalid token")
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

async def process_chapter_generation_task(
    payload: ChapterGenerationTaskPayload,
    chapter_service: ChapterService,
    supabase: Client
):
    """
    Process the chapter generation task. This replaces the trigger_chapter_generation function.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0
    failures_detected = False

    log.info(f"[Cloud Task] Starting chapter generation for order_id: {order_id} (retry: {retry_count})")
    
    # Fetch order details from database
    try:
        log.info(f"[Cloud Task] Fetching order details for order_id: {order_id}")
        order_response = supabase.table("orders").select("*, order_items(*)").eq("id", order_id).single().execute()
        
        if not order_response.data:
            log.error(f"[Cloud Task] Order not found for order_id: {order_id}")
            raise Exception(f"Order {order_id} not found in database")
        
        order_data = order_response.data
        user_id = order_data.get("user_id")
        order_items = order_data.get("order_items", [])
        
        if not user_id:
            log.error(f"[Cloud Task] No user_id found for order_id: {order_id}")
            raise Exception(f"No user_id found for order {order_id}")
        
        if not order_items:
            log.warning(f"[Cloud Task] No order items found for order_id: {order_id}")
            return  # Nothing to process
            
        log.info(f"[Cloud Task] Found {len(order_items)} items for order_id: {order_id}, user_id: {user_id}")
        
    except Exception as e:
        log.error(f"[Cloud Task] Failed to fetch order details for order_id: {order_id}. Error: {e}", exc_info=True)
        raise

    try:
        log.info(f"[Cloud Task] Updating order {order_id} status to 'generating'.")
        supabase.table("orders").update({"status": "generating"}).eq("id", order_id).execute()
    except Exception as e:
        log.error(f"[Cloud Task] Failed to update order {order_id} status to 'generating'. Error: {e}", exc_info=True)
    
    # Update all books to 'generating' status
    try:
        log.info(f"[Cloud Task] Updating all books in order {order_id} to 'generating' status.")
        chapter_service.update_books_status_for_order(order_items, "generating")
    except Exception as e:
        log.error(f"[Cloud Task] Failed to update books status to 'generating' for order {order_id}. Error: {e}", exc_info=True)
    
    log.info(f"[Cloud Task] Starting chapter generation for order_id: {order_id} with {len(order_items)} item(s).")
    
    for item in order_items:
        book_id = item.get("book_id")
        if not book_id:
            log.warning(f"[Cloud Task] Skipping item with no book_id in order {order_id}.")
            continue
        
        try:   
            log.info(f"[Cloud Task] Generating all chapters for book_id: {book_id} from order {order_id}.")
            chapter_service.generate_all_chapters(
                user_id=user_id,
                wish_id=None,
                book_id=book_id
            )
            log.info(f"[Cloud Task] Successfully generated all chapters for book_id: {book_id}.")
            
            # Update this specific book to 'completed' status
            try:
                chapter_service.update_book_status(book_id, "completed")
                log.info(f"[Cloud Task] Successfully updated book_id: {book_id} status to 'completed'.")
                
                # Check if this is a non-subscription book and handle PDF generation + email
                try:
                    await handle_non_subscription_completion(
                        order_id=order_id,
                        book_id=book_id,
                        user_id=user_id,
                        supabase=supabase
                    )
                except Exception as pdf_email_error:
                    log.error(f"[Cloud Task] Failed to handle PDF generation/email for book_id: {book_id}. Error: {pdf_email_error}", exc_info=True)
                    # Don't mark the entire order as failed for PDF/email issues
                    
            except Exception as e:
                log.error(f"[Cloud Task] Failed to update book_id: {book_id} status to 'completed'. Error: {e}", exc_info=True)
                failures_detected = True
                
        except Exception as e:
            log.error(f"[Cloud Task] Failed to generate chapters for book_id: {book_id} in order {order_id}. Error: {e}", exc_info=True)
            failures_detected = True
            
            # Update this specific book to 'failed' status
            try:
                chapter_service.update_book_status(book_id, "failed")
                log.info(f"[Cloud Task] Updated book_id: {book_id} status to 'failed' due to generation error.")
            except Exception as update_error:
                log.error(f"[Cloud Task] Failed to update book_id: {book_id} status to 'failed'. Error: {update_error}", exc_info=True)

    final_status = "failed" if failures_detected else "completed"
    log.info(f"[Cloud Task] All generation tasks finished for order {order_id}. Setting final status to '{final_status}'.")
    try:
        supabase.table("orders").update({"status": final_status}).eq("id", order_id).execute()
        log.info(f"[Cloud Task] Successfully updated order {order_id} to status '{final_status}'.")
    except Exception as e:
        log.error(f"[Cloud Task] CRITICAL: Failed to update final status for order {order_id}. Error: {e}", exc_info=True)

    log.info(f"[Cloud Task] Finished processing order_id: {order_id}.")

# --- API Routes ---

@router.post("/generate-chapters", summary="Cloud Tasks handler for chapter generation")
async def handle_chapter_generation_task(
    payload: ChapterGenerationTaskPayload,
    _: bool = Depends(verify_task_authorization),
    chapter_service: ChapterService = Depends(get_chapter_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle chapter generation tasks from Google Cloud Tasks.
    This endpoint is called by Cloud Tasks to process chapter generation jobs.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0
    max_retries = 3
    
    log.info(f"[Cloud Task Handler] Received chapter generation task for order_id: {order_id} (retry: {retry_count})")
    
    try:
        # Process the chapter generation
        await process_chapter_generation_task(
            payload=payload,
            chapter_service=chapter_service,
            supabase=supabase
        )
        
        log.info(f"[Cloud Task Handler] Successfully completed chapter generation for order_id: {order_id}")
        return {
            "status": "success",
            "message": f"Chapter generation completed for order {order_id}",
            "order_id": order_id,
            "retry_count": retry_count
        }
        
    except Exception as e:
        log.error(f"[Cloud Task Handler] Chapter generation failed for order_id: {order_id} (retry: {retry_count}). Error: {e}", exc_info=True)
        
        # If we haven't exceeded max retries, create a retry task
        if retry_count < max_retries:
            try:
                retry_task_name = cloud_tasks_service.create_retry_task(
                    order_id=order_id,
                    retry_count=retry_count + 1,
                    delay_seconds=300 * (retry_count + 1)  # Exponential backoff: 5, 10, 15 minutes
                )
                log.info(f"[Cloud Task Handler] Created retry task {retry_task_name} for order_id: {order_id}")
                
                return {
                    "status": "retry_scheduled",
                    "message": f"Task failed, retry scheduled (attempt {retry_count + 1}/{max_retries})",
                    "order_id": order_id,
                    "retry_count": retry_count + 1,
                    "retry_task_name": retry_task_name
                }
            except Exception as retry_error:
                log.error(f"[Cloud Task Handler] Failed to create retry task for order_id: {order_id}. Error: {retry_error}", exc_info=True)
        
        # Mark order as failed if we've exceeded retries or can't create retry task
        try:
            supabase.table("orders").update({"status": "failed"}).eq("id", order_id).execute()
            log.info(f"[Cloud Task Handler] Marked order {order_id} as failed after {retry_count + 1} attempts")
        except Exception as update_error:
            log.error(f"[Cloud Task Handler] Failed to mark order {order_id} as failed. Error: {update_error}", exc_info=True)
        
        # Return error response
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chapter generation failed for order {order_id} after {retry_count + 1} attempts"
        )

@router.get("/queue-info", summary="Get Cloud Tasks queue information")
def get_queue_info(
    _: bool = Depends(verify_task_authorization),
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
