# app/api/routes/tasks.py
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from typing import Dict, Any, Optional
from pydantic import BaseModel
from supabase import Client
from datetime import datetime
import json
import jwt
import os
from jwt import PyJWKClient

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

class SingleChapterTaskPayload(BaseModel):
    chapter_id: str
    order_id: str
    book_id: str
    user_id: str
    module_index: int
    topic_index: int
    idx: int

class CompletionMonitoringTaskPayload(BaseModel):
    order_id: str

# --- Dependencies ---
def get_chapter_service(supabase: Client = Depends(get_supabase)) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    from openai import OpenAI
    return ChapterService(supabase=supabase, openai_client=OpenAI())

def get_cloud_tasks_service() -> CloudTasksService:
    """Dependency to provide a CloudTasksService instance."""
    return CloudTasksService()

def verify_task_authorization_oidc(authorization: str = Header(None), request: Request = None) -> bool:
    """Verify that the request is authorized using Google Cloud OIDC token."""
    if not authorization:
        log.warning("Task endpoint called without authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header"
        )
    
    # Extract token from "Bearer <token>" format
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            raise ValueError("Invalid scheme")
    except ValueError:
        log.warning(f"Task endpoint called with invalid authorization format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )
    
    try:
        # Verify the OIDC token
        # For Google Cloud Tasks, we need to verify the JWT token
        jwks_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        # Get the expected audience from the request URL
        audience = str(request.url) if request else f"{settings.BACKEND_URL}/tasks"
        
        # Decode and verify the token (without strict audience check for now)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},  # We'll verify audience manually
            issuer="https://accounts.google.com"
        )
        
        # Verify the service account email
        expected_email = settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com"
        if payload.get("email") != expected_email:
            log.warning(f"Invalid service account email in token: {payload.get('email')}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid service account"
            )
        
        log.debug(f"Successfully verified OIDC token for service account: {payload.get('email')}")
        return True
        
    except jwt.InvalidTokenError as e:
        log.warning(f"Invalid OIDC token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OIDC token"
        )
    except Exception as e:
        log.error(f"Error verifying OIDC token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed"
        )

def verify_task_authorization_legacy(authorization: str = Header(None)) -> bool:
    """Legacy verification using custom Bearer token (for backward compatibility)."""
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
        log.warning(f"Task endpoint called with invalid authorization format: {authorization}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )
    
    # Verify the token matches our job runner secret
    expected_secret = settings.JOB_RUNNER_SECRET
    if token != expected_secret:
        log.warning(f"Task endpoint called with invalid token. Expected length: {len(expected_secret) if expected_secret else 'None'}, Received length: {len(token)}")
        log.debug(f"Expected secret (first 10 chars): {expected_secret[:10] if expected_secret else 'None'}...")
        log.debug(f"Received token (first 10 chars): {token[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token"
        )
    
    return True

def verify_task_authorization(authorization: str = Header(None), request: Request = None) -> bool:
    """
    Verify task authorization using OIDC token with fallback to legacy token.
    This provides backward compatibility while transitioning to OIDC.
    """
    try:
        # Try OIDC verification first
        return verify_task_authorization_oidc(authorization, request)
    except HTTPException as oidc_error:
        # If OIDC fails, try legacy verification as fallback
        try:
            log.debug("OIDC verification failed, trying legacy token verification")
            return verify_task_authorization_legacy(authorization)
        except HTTPException as legacy_error:
            # Both methods failed, return the OIDC error (preferred method)
            log.warning("Both OIDC and legacy token verification failed")
            raise oidc_error

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
    cloud_tasks_service: CloudTasksService,
    supabase: Client
):
    """
    Process the chapter generation task using the new parallel approach.
    This creates individual chapter generation tasks and monitors completion.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0

    log.info(f"[Cloud Task] Starting parallel chapter generation for order_id: {order_id} (retry: {retry_count})")
    
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
    
    log.info(f"[Cloud Task] Starting parallel chapter preparation for order_id: {order_id} with {len(order_items)} item(s).")
    
    total_tasks_created = 0
    
    # Phase 1: Prepare all chapters in database and create individual generation tasks
    for item in order_items:
        book_id = item.get("book_id")
        if not book_id:
            log.warning(f"[Cloud Task] Skipping item with no book_id in order {order_id}.")
            continue
        
        try:   
            log.info(f"[Cloud Task] Preparing chapters for parallel generation - book_id: {book_id} from order {order_id}.")
            
            # Prepare chapters in database and get task information
            chapter_tasks = chapter_service.prepare_chapters_for_parallel_generation(
                user_id=user_id,
                order_id=order_id,
                book_id=book_id
            )
            
            log.info(f"[Cloud Task] Prepared {len(chapter_tasks)} chapters for book_id: {book_id}. Creating individual tasks.")
            
            # Create individual Cloud Tasks for each chapter
            for task_info in chapter_tasks:
                try:
                    # Check if this chapter already has content_path (already generated)
                    chapter_check = supabase.table("chapters").select("content_path").eq("id", task_info["chapter_id"]).single().execute()
                    
                    if chapter_check.data and chapter_check.data.get("content_path"):
                        log.info(f"[Cloud Task] Chapter {task_info['idx']} (ID: {task_info['chapter_id']}) already has content, skipping task creation")
                        continue
                    
                    log.debug(f"[Cloud Task] Chapter {task_info['idx']} (ID: {task_info['chapter_id']}) has no content_path, creating generation task")
                    
                    task_name = cloud_tasks_service.create_individual_chapter_task(
                        chapter_id=task_info["chapter_id"],
                        order_id=task_info["order_id"],
                        book_id=task_info["book_id"],
                        user_id=task_info["user_id"],
                        module_index=task_info["module_index"],
                        topic_index=task_info["topic_index"],
                        idx=task_info["idx"]
                    )
                    total_tasks_created += 1
                    log.info(f"[Cloud Task] Created individual chapter task: {task_name} for chapter {task_info['idx']} (ID: {task_info['chapter_id']})")
                    
                except Exception as task_error:
                    log.error(f"[Cloud Task] Failed to create individual task for chapter {task_info['idx']} (ID: {task_info['chapter_id']}). Error: {task_error}", exc_info=True)
                    # Mark this chapter as failed
                    try:
                        supabase.table("chapters").update({
                            "status": "failed",
                            "error_message": f"Failed to create generation task: {str(task_error)}"
                        }).eq("id", task_info["chapter_id"]).execute()
                    except Exception as update_error:
                        log.error(f"[Cloud Task] Failed to mark chapter as failed: {update_error}", exc_info=True)
                
        except Exception as e:
            log.error(f"[Cloud Task] Failed to prepare chapters for book_id: {book_id} in order {order_id}. Error: {e}", exc_info=True)
            # Mark this book as failed
            try:
                chapter_service.update_book_status(book_id, "failed")
                log.info(f"[Cloud Task] Updated book_id: {book_id} status to 'failed' due to preparation error.")
            except Exception as update_error:
                log.error(f"[Cloud Task] Failed to update book_id: {book_id} status to 'failed'. Error: {update_error}", exc_info=True)

    log.info(f"[Cloud Task] Created {total_tasks_created} individual chapter generation tasks for order {order_id}.")
    
    # Phase 2: Create completion monitoring task to check when all chapters are done
    if total_tasks_created > 0:
        try:
            # Schedule first completion check after a reasonable delay (30 seconds)
            monitoring_task_name = cloud_tasks_service.create_completion_monitoring_task(
                order_id=order_id,
                delay_seconds=30
            )
            log.info(f"[Cloud Task] Created completion monitoring task: {monitoring_task_name} for order {order_id}")
            
        except Exception as monitor_error:
            log.error(f"[Cloud Task] Failed to create completion monitoring task for order {order_id}. Error: {monitor_error}", exc_info=True)
            # This is critical - without monitoring, the order might stay in "generating" status forever
            # Fall back to marking as failed
            try:
                supabase.table("orders").update({"status": "failed", "error_message": "Failed to create completion monitoring"}).eq("id", order_id).execute()
            except Exception as fallback_error:
                log.error(f"[Cloud Task] Failed to update order status as fallback. Error: {fallback_error}", exc_info=True)
    else:
        # No tasks were created, mark order as failed
        log.error(f"[Cloud Task] No chapter generation tasks were created for order {order_id}.")
        try:
            supabase.table("orders").update({"status": "failed", "error_message": "No chapters could be prepared for generation"}).eq("id", order_id).execute()
        except Exception as e:
            log.error(f"[Cloud Task] Failed to mark order as failed. Error: {e}", exc_info=True)

    log.info(f"[Cloud Task] Finished setting up parallel chapter generation for order_id: {order_id}.")

async def process_chapter_generation_task_legacy(
    payload: ChapterGenerationTaskPayload,
    chapter_service: ChapterService,
    supabase: Client
):
    """
    Legacy sequential chapter generation for backward compatibility.
    This is the original implementation that can be used as a fallback.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0
    failures_detected = False

    log.info(f"[Cloud Task] Starting LEGACY sequential chapter generation for order_id: {order_id} (retry: {retry_count})")
    
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
    
    log.info(f"[Cloud Task] Starting sequential chapter generation for order_id: {order_id} with {len(order_items)} item(s).")
    
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
    request: Request,
    _: bool = Depends(verify_task_authorization),
    chapter_service: ChapterService = Depends(get_chapter_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle chapter generation tasks from Google Cloud Tasks.
    This endpoint uses the new parallel chapter generation approach by default.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0
    max_retries = 3
    
    log.info(f"[Cloud Task Handler] Received chapter generation task for order_id: {order_id} (retry: {retry_count})")
    
    try:
        # Use the new parallel approach by default
        await process_chapter_generation_task(
            payload=payload,
            chapter_service=chapter_service,
            cloud_tasks_service=cloud_tasks_service,
            supabase=supabase
        )
        
        log.info(f"[Cloud Task Handler] Successfully set up parallel chapter generation for order_id: {order_id}")
        return {
            "status": "success",
            "message": f"Parallel chapter generation initiated for order {order_id}",
            "order_id": order_id,
            "retry_count": retry_count
        }
        
    except Exception as e:
        log.error(f"[Cloud Task Handler] Parallel chapter generation setup failed for order_id: {order_id} (retry: {retry_count}). Error: {e}", exc_info=True)
        
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

@router.post("/generate-chapters-legacy", summary="Cloud Tasks handler for legacy sequential chapter generation")
async def handle_chapter_generation_task_legacy(
    payload: ChapterGenerationTaskPayload,
    _: bool = Depends(verify_task_authorization),
    chapter_service: ChapterService = Depends(get_chapter_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle chapter generation tasks using the legacy sequential approach.
    This endpoint is available for backward compatibility or as a fallback.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0
    max_retries = 3
    
    log.info(f"[Cloud Task Handler Legacy] Received LEGACY chapter generation task for order_id: {order_id} (retry: {retry_count})")
    
    try:
        # Use the legacy sequential approach
        await process_chapter_generation_task_legacy(
            payload=payload,
            chapter_service=chapter_service,
            supabase=supabase
        )
        
        log.info(f"[Cloud Task Handler Legacy] Successfully completed LEGACY chapter generation for order_id: {order_id}")
        return {
            "status": "success",
            "message": f"Legacy chapter generation completed for order {order_id}",
            "order_id": order_id,
            "retry_count": retry_count
        }
        
    except Exception as e:
        log.error(f"[Cloud Task Handler Legacy] LEGACY chapter generation failed for order_id: {order_id} (retry: {retry_count}). Error: {e}", exc_info=True)
        
        # If we haven't exceeded max retries, create a retry task
        if retry_count < max_retries:
            try:
                # Create retry task that calls the legacy endpoint
                retry_payload = {
                    "order_id": order_id,
                    "retry_count": retry_count + 1
                }
                
                task = {
                    "http_request": {
                        "http_method": "POST",
                        "url": f"{settings.BACKEND_URL}/tasks/generate-chapters-legacy",
                        "headers": {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {settings.JOB_RUNNER_SECRET}"
                        },
                        "body": json.dumps(retry_payload).encode("utf-8"),
                    },
                }
                
                # This is a simplified retry creation for the legacy endpoint
                # In a real implementation, you might want to extract this to the cloud_tasks_service
                response = cloud_tasks_service.client.create_task(
                    request={"parent": cloud_tasks_service.parent, "task": task}
                )
                retry_task_name = response.name
                
                log.info(f"[Cloud Task Handler Legacy] Created legacy retry task {retry_task_name} for order_id: {order_id}")
                
                return {
                    "status": "retry_scheduled",
                    "message": f"Legacy task failed, retry scheduled (attempt {retry_count + 1}/{max_retries})",
                    "order_id": order_id,
                    "retry_count": retry_count + 1,
                    "retry_task_name": retry_task_name
                }
            except Exception as retry_error:
                log.error(f"[Cloud Task Handler Legacy] Failed to create legacy retry task for order_id: {order_id}. Error: {retry_error}", exc_info=True)
        
        # Mark order as failed if we've exceeded retries or can't create retry task
        try:
            supabase.table("orders").update({"status": "failed"}).eq("id", order_id).execute()
            log.info(f"[Cloud Task Handler Legacy] Marked order {order_id} as failed after {retry_count + 1} attempts")
        except Exception as update_error:
            log.error(f"[Cloud Task Handler Legacy] Failed to mark order {order_id} as failed. Error: {update_error}", exc_info=True)
        
        # Return error response
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Legacy chapter generation failed for order {order_id} after {retry_count + 1} attempts"
        )

@router.post("/generate-single-chapter", summary="Cloud Tasks handler for individual chapter generation")
async def handle_single_chapter_generation_task(
    payload: SingleChapterTaskPayload,
    request: Request,
    _: bool = Depends(verify_task_authorization),
    chapter_service: ChapterService = Depends(get_chapter_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle individual chapter generation tasks from Google Cloud Tasks.
    This endpoint generates content for a single chapter in parallel.
    """
    chapter_id = payload.chapter_id
    order_id = payload.order_id
    book_id = payload.book_id
    user_id = payload.user_id
    module_index = payload.module_index
    topic_index = payload.topic_index
    idx = payload.idx
    
    log.info(f"[Single Chapter Task] Processing chapter_id: {chapter_id}, idx: {idx} for order_id: {order_id}")
    
    try:
        # Generate content for this specific chapter
        result = chapter_service.generate_chapter(
            user_id=user_id,
            wish_id=None,
            book_id=book_id,
            idx=idx,
            module_index=module_index,
            topic_index=topic_index
        )
        
        log.info(f"[Single Chapter Task] Successfully generated chapter_id: {chapter_id} for order_id: {order_id}")
        return {
            "status": "success",
            "message": f"Chapter {idx} generated successfully",
            "chapter_id": chapter_id,
            "order_id": order_id,
            "result": result
        }
        
    except Exception as e:
        log.error(f"[Single Chapter Task] Failed to generate chapter_id: {chapter_id}. Error: {e}", exc_info=True)
        
        # Mark this specific chapter as failed
        try:
            supabase.table("chapters").update({
                "status": "failed",
                "error_message": str(e),
                "updated_at": datetime.now().isoformat()
            }).eq("id", chapter_id).execute()
            log.info(f"[Single Chapter Task] Marked chapter_id: {chapter_id} as failed")
        except Exception as update_error:
            log.error(f"[Single Chapter Task] Failed to update chapter status for chapter_id: {chapter_id}. Error: {update_error}", exc_info=True)
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chapter generation failed for chapter {chapter_id}: {str(e)}"
        )

@router.post("/monitor-completion", summary="Cloud Tasks handler for monitoring chapter completion")
async def handle_completion_monitoring_task(
    payload: CompletionMonitoringTaskPayload,
    request: Request,
    _: bool = Depends(verify_task_authorization),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Monitor chapter completion and trigger final processing when all chapters are complete.
    This endpoint checks if all chapters for an order have been generated and triggers PDF/email processing.
    """
    order_id = payload.order_id
    log.info(f"[Completion Monitor] Checking completion status for order_id: {order_id}")
    
    try:
        # Get all chapters for this order
        order_response = supabase.table("orders").select("*, order_items(*)").eq("id", order_id).single().execute()
        if not order_response.data:
            log.error(f"[Completion Monitor] Order not found: {order_id}")
            raise HTTPException(status_code=404, detail="Order not found")
        
        order_data = order_response.data
        order_items = order_data.get("order_items", [])
        user_id = order_data.get("user_id")
        
        if not order_items:
            log.warning(f"[Completion Monitor] No order items found for order_id: {order_id}")
            return {"status": "no_items", "message": "No items to process"}
        
        # Check completion status for all books in the order
        all_completed = True
        total_chapters = 0
        completed_chapters = 0
        failed_chapters = 0
        
        for item in order_items:
            book_id = item.get("book_id")
            if not book_id:
                continue
                
            # Get all chapters for this book
            chapters_response = supabase.table("chapters").select("id, content_path, status").eq("book_id", book_id).execute()
            book_chapters = chapters_response.data
            
            for chapter in book_chapters:
                total_chapters += 1
                if chapter.get("content_path"):  # Chapter has been generated
                    completed_chapters += 1
                elif chapter.get("status") == "failed":
                    failed_chapters += 1
                else:
                    all_completed = False
        
        log.info(f"[Completion Monitor] Order {order_id}: {completed_chapters}/{total_chapters} chapters completed, {failed_chapters} failed")
        
        if all_completed and total_chapters > 0:
            # All chapters are complete, trigger final processing
            log.info(f"[Completion Monitor] All chapters completed for order {order_id}, starting final processing")
            
            try:
                # Update order status to completed and clear monitoring task ID
                supabase.table("orders").update({
                    "status": "completed",
                    "monitoring_task_id": None
                }).eq("id", order_id).execute()
                
                # Update all books in the order to completed status
                for item in order_items:
                    book_id = item.get("book_id")
                    if book_id:
                        try:
                            supabase.table("books").update({"status": "completed"}).eq("id", book_id).execute()
                            log.info(f"[Completion Monitor] Updated book_id: {book_id} status to 'completed'")
                            
                            # Handle PDF generation and email for non-subscription books
                            await handle_non_subscription_completion(
                                order_id=order_id,
                                book_id=book_id,
                                user_id=user_id,
                                supabase=supabase
                            )
                        except Exception as book_error:
                            log.error(f"[Completion Monitor] Failed to process book_id: {book_id}. Error: {book_error}", exc_info=True)
                
                log.info(f"[Completion Monitor] Successfully completed final processing for order {order_id}")
                return {
                    "status": "completed",
                    "message": "All chapters completed, final processing done",
                    "order_id": order_id,
                    "total_chapters": total_chapters,
                    "completed_chapters": completed_chapters,
                    "failed_chapters": failed_chapters
                }
                
            except Exception as processing_error:
                log.error(f"[Completion Monitor] Failed final processing for order {order_id}. Error: {processing_error}", exc_info=True)
                # Mark order as failed and clear monitoring task ID
                supabase.table("orders").update({
                    "status": "failed",
                    "monitoring_task_id": None
                }).eq("id", order_id).execute()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Final processing failed"
                )
        
        elif failed_chapters > 0 and (completed_chapters + failed_chapters) == total_chapters:
            # Some chapters failed but all processing is done
            log.warning(f"[Completion Monitor] Order {order_id} completed with {failed_chapters} failed chapters")
            supabase.table("orders").update({
                "status": "completed_with_errors",
                "monitoring_task_id": None
            }).eq("id", order_id).execute()
            
            # Still trigger processing for completed chapters
            for item in order_items:
                book_id = item.get("book_id")
                if book_id:
                    try:
                        # Check if this book has any completed chapters
                        book_chapters_response = supabase.table("chapters").select("content_path").eq("book_id", book_id).execute()
                        has_completed_chapters = any(ch.get("content_path") for ch in book_chapters_response.data)
                        
                        if has_completed_chapters:
                            supabase.table("books").update({"status": "completed_with_errors"}).eq("id", book_id).execute()
                            await handle_non_subscription_completion(
                                order_id=order_id,
                                book_id=book_id,
                                user_id=user_id,
                                supabase=supabase
                            )
                        else:
                            supabase.table("books").update({"status": "failed"}).eq("id", book_id).execute()
                    except Exception as book_error:
                        log.error(f"[Completion Monitor] Failed to process book_id: {book_id}. Error: {book_error}", exc_info=True)
            
            return {
                "status": "completed_with_errors",
                "message": "Processing completed with some failures",
                "order_id": order_id,
                "total_chapters": total_chapters,
                "completed_chapters": completed_chapters,
                "failed_chapters": failed_chapters
            }
        
        else:
            # Not all chapters are complete yet, schedule another check
            log.info(f"[Completion Monitor] Order {order_id} not yet complete, scheduling another check")
            
            try:
                # Schedule another monitoring task with exponential backoff
                next_check_delay = min(300, 60 * 2)  # Start with 2 minutes, max 5 minutes
                task_name = cloud_tasks_service.create_completion_monitoring_task(
                    order_id=order_id,
                    delay_seconds=next_check_delay
                )
                log.info(f"[Completion Monitor] Scheduled next check for order {order_id}: {task_name}")
                
                return {
                    "status": "pending",
                    "message": "Chapters still generating, next check scheduled",
                    "order_id": order_id,
                    "total_chapters": total_chapters,
                    "completed_chapters": completed_chapters,
                    "failed_chapters": failed_chapters,
                    "next_check_task": task_name
                }
                
            except Exception as schedule_error:
                log.error(f"[Completion Monitor] Failed to schedule next check for order {order_id}. Error: {schedule_error}", exc_info=True)
                return {
                    "status": "monitoring_failed",
                    "message": "Failed to schedule next completion check",
                    "order_id": order_id
                }
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        log.error(f"[Completion Monitor] Unexpected error monitoring order {order_id}. Error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to monitor completion for order {order_id}"
        )

@router.get("/debug-config", summary="Debug configuration (remove in production)")
def debug_config():
    """
    Debug endpoint to check configuration. REMOVE THIS IN PRODUCTION!
    This endpoint does not require authentication for debugging purposes.
    """
    try:
        config_info = {
            "backend_url": settings.BACKEND_URL,
            "gcp_project_id": settings.GCP_PROJECT_ID,
            "gcp_location": settings.GCP_LOCATION,
            "gcp_queue_name": settings.GCP_QUEUE_NAME,
            "service_account_email": settings.CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL or "task-invoker@lateral-berm-471911-k2.iam.gserviceaccount.com",
            "service_account_key_path": settings.GCP_SERVICE_ACCOUNT_KEY_PATH,
            "service_account_key_exists": os.path.exists(settings.GCP_SERVICE_ACCOUNT_KEY_PATH),
            "authentication_method": "OIDC (Google Service Account)",
            "job_runner_secret_configured": bool(settings.JOB_RUNNER_SECRET),
            "job_runner_secret_length": len(settings.JOB_RUNNER_SECRET) if settings.JOB_RUNNER_SECRET else 0,
            "job_runner_secret_preview": settings.JOB_RUNNER_SECRET[:10] + "..." if settings.JOB_RUNNER_SECRET else "None"
        }
        log.info(f"Debug config requested: {config_info}")
        return config_info
    except Exception as e:
        log.error(f"Failed to get debug config: {e}", exc_info=True)
        return {"error": str(e)}

@router.get("/queue-info", summary="Get Cloud Tasks queue information")
def get_queue_info(
    request: Request,
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
