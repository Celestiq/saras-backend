# app/api/routes/tasks.py
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from typing import Dict, Any, Optional
from pydantic import BaseModel
from supabase import Client
from datetime import datetime, timezone
import json
import os

from app.db.supabase import get_supabase
from app.services.chapter_service import ChapterService
from app.services.cloud_tasks_service import CloudTasksService
from app.core.logging import get_logger
from app.core.config import settings
from app.api.auth.task_auth import verify_task_authorization
from app.api.helpers.task_helpers import handle_non_subscription_completion, check_order_completion_immediately
from app.api.processors.task_processors import process_chapter_generation_task, process_chapter_generation_task_legacy

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

class PDFGenerationTaskPayload(BaseModel):
    """Payload for PDF generation tasks."""
    order_id: str
    book_id: str
    user_id: str
    book_title: str

# --- Dependencies ---
def get_chapter_service(supabase: Client = Depends(get_supabase)) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    from openai import OpenAI
    return ChapterService(supabase=supabase, openai_client=OpenAI())

def get_cloud_tasks_service() -> CloudTasksService:
    """Dependency to provide a CloudTasksService instance."""
    return CloudTasksService()

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
            supabase.table("orders").update({"status": "failed", "generation_task_id": None}).eq("id", order_id).execute()
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
            supabase.table("orders").update({"status": "failed", "generation_task_id": None}).eq("id", order_id).execute()
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
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
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
        # Check if chapter is already being processed by another task
        try:
            chapter_check = supabase.table("chapters").select("status, content_path, generation_task_id").eq("id", chapter_id).single().execute()
            if chapter_check.data:
                chapter_data = chapter_check.data
                current_status = chapter_data.get("status")
                content_path = chapter_data.get("content_path")
                
                # If chapter already has content, skip processing
                if content_path:
                    log.warning(f"[Single Chapter Task] Chapter {chapter_id} (idx: {idx}) already has content, skipping generation")
                    return {
                        "status": "skipped",
                        "message": f"Chapter {idx} already completed",
                        "chapter_id": chapter_id,
                        "order_id": order_id
                    }
                
                # If chapter is not in generating status, update it
                if current_status != "generating":
                    supabase.table("chapters").update({
                        "status": "generating",
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", chapter_id).execute()
                    log.info(f"[Single Chapter Task] Updated chapter {chapter_id} status to 'generating'")
        except Exception as status_error:
            log.warning(f"[Single Chapter Task] Failed to check/update chapter status: {status_error}")
        
        # Generate content for this specific chapter
        result = chapter_service.generate_chapter(
            user_id=user_id,
            wish_id=None,
            book_id=book_id,
            idx=idx,
            module_index=module_index,
            topic_index=topic_index
        )
        
        # Update chapter status to completed and clear generation task ID
        try:
            supabase.table("chapters").update({
                "status": "completed",
                "generation_task_id": None,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", chapter_id).execute()
            log.info(f"[Single Chapter Task] Updated chapter {chapter_id} status to 'completed' and cleared task ID")
        except Exception as update_error:
            log.warning(f"[Single Chapter Task] Failed to update chapter status: {update_error}")
        
        log.info(f"[Single Chapter Task] Successfully generated chapter_id: {chapter_id} for order_id: {order_id}")
        
        # Trigger immediate completion check to avoid timing gaps
        try:
            log.info(f"[Single Chapter Task] Triggering immediate completion check for order_id: {order_id}")
            await check_order_completion_immediately(order_id, supabase, cloud_tasks_service)
        except Exception as completion_error:
            log.warning(f"[Single Chapter Task] Failed to trigger immediate completion check for order_id: {order_id}. Error: {completion_error}")
            # Don't fail the chapter generation if completion check fails
        
        return {
            "status": "success",
            "message": f"Chapter {idx} generated successfully",
            "chapter_id": chapter_id,
            "order_id": order_id,
            "result": result
        }
        
    except Exception as e:
        log.error(f"[Single Chapter Task] Failed to generate chapter_id: {chapter_id}. Error: {e}", exc_info=True)
        
        # Mark this specific chapter as failed and clear generation task ID
        try:
            supabase.table("chapters").update({
                "status": "failed",
                "error_message": str(e),
                "generation_task_id": None,
                "updated_at": datetime.now().isoformat()
            }).eq("id", chapter_id).execute()
            log.info(f"[Single Chapter Task] Marked chapter_id: {chapter_id} as failed and cleared task ID")
        except Exception as update_error:
            log.error(f"[Single Chapter Task] Failed to update chapter status for chapter_id: {chapter_id}. Error: {update_error}", exc_info=True)
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chapter generation failed for chapter {chapter_id}: {str(e)}"
        )

@router.post("/generate-pdf", summary="Cloud Tasks handler for PDF generation")
async def handle_pdf_generation_task(
    payload: PDFGenerationTaskPayload,
    request: Request,
    _: bool = Depends(verify_task_authorization),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle PDF generation tasks from Google Cloud Tasks.
    This endpoint generates PDF and sends completion email asynchronously.
    """
    order_id = payload.order_id
    book_id = payload.book_id
    user_id = payload.user_id
    book_title = payload.book_title
    
    log.info(f"[PDF Generation Task] Processing PDF generation for book_id: {book_id}, order_id: {order_id}")
    
    try:
        # Get user email from profiles table
        user_email = None
        user_name = "User"  # Default name
        
        try:
            user_response = supabase.table("profiles").select("email, full_name").eq("user_id", user_id).single().execute()
            if user_response.data:
                user_email = user_response.data.get("email")
                user_name = user_response.data.get("full_name", "User")
                log.info(f"[PDF Generation Task] Found user email: {user_email} for user_id: {user_id}")
            else:
                log.error(f"[PDF Generation Task] No profile found for user_id: {user_id}")
                return {"status": "error", "message": "User profile not found"}
        except Exception as e:
            log.error(f"[PDF Generation Task] Failed to fetch user profile for user_id: {user_id}. Error: {e}", exc_info=True)
            return {"status": "error", "message": "Failed to fetch user profile"}
        
        if not user_email:
            log.error(f"[PDF Generation Task] No email found for user_id: {user_id}")
            return {"status": "error", "message": "No email found for user"}
        
        # Generate PDF using the PDF service
        try:
            from app.services.pdf_service import PDFService
            pdf_service = PDFService(supabase)
            
            log.info(f"[PDF Generation Task] Starting PDF generation for book_id: {book_id}")
            pdf_result = pdf_service.create_book_pdf(
                book_id=book_id,
                book_title=book_title,
                subscription=False
            )
            
            if pdf_result.get("status") == "success":
                pdf_url = pdf_result.get("storage_url")
                log.info(f"[PDF Generation Task] PDF generated successfully for book_id: {book_id}. URL: {pdf_url}")
            else:
                log.error(f"[PDF Generation Task] PDF generation failed for book_id: {book_id}. Result: {pdf_result}")
                return {"status": "error", "message": "PDF generation failed"}
                
        except Exception as pdf_error:
            log.error(f"[PDF Generation Task] PDF generation failed for book_id: {book_id}. Error: {pdf_error}", exc_info=True)
            return {"status": "error", "message": f"PDF generation failed: {str(pdf_error)}"}
        
        # Send completion email with PDF
        try:
            from app.services.email_service import EmailService
            email_service = EmailService()
            
            # Prepare email content
            subject = f"Your Book: {book_title} is Ready!"
            html_content = f"""
            <html>
            <body>
                <h2>Your Book is Ready!</h2>
                <p>Hello {user_name}!</p>
                <p>Your book "{book_title}" has been generated and is ready for download. Please find the PDF attached to this email.</p>
                <p>Thank you for using our service!</p>
                <br>
                <p>Best regards,<br>The SARAS Team</p>
            </body>
            </html>
            """
            
            # Send email with PDF attachment
            email_result = email_service.send_email_with_attachment(
                recipient_email=user_email,
                subject=subject,
                html_content=html_content,
                attachment_path=pdf_url
            )
            
            if email_result.get("success", True):
                log.info(f"[PDF Generation Task] Completion email with PDF sent successfully to {user_email} for order {order_id}")
                
                # Update order status to completed
                try:
                    supabase.table("orders").update({
                        "status": "completed",
                        "generation_task_id": None,
                        "monitoring_task_id": None,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", order_id).execute()
                    log.info(f"[PDF Generation Task] Updated order {order_id} status to 'completed'")
                except Exception as update_error:
                    log.warning(f"[PDF Generation Task] Failed to update order status: {update_error}")
                
                return {
                    "status": "success",
                    "message": f"PDF generated and email sent successfully for book {book_title}",
                    "book_id": book_id,
                    "order_id": order_id,
                    "pdf_url": pdf_url
                }
            else:
                log.error(f"[PDF Generation Task] Failed to send completion email with PDF to {user_email} for order {order_id}. Result: {email_result}")
                return {"status": "error", "message": "Failed to send completion email"}
                
        except Exception as email_error:
            log.error(f"[PDF Generation Task] Error sending completion email with PDF for order {order_id}: {email_error}", exc_info=True)
            return {"status": "error", "message": f"Email sending failed: {str(email_error)}"}
        
    except Exception as e:
        log.error(f"[PDF Generation Task] Unexpected error during PDF generation for book_id: {book_id}. Error: {e}", exc_info=True)
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}

@router.post("/trigger-completion-check", summary="Manually trigger completion check for an order")
async def trigger_completion_check(
    order_id: str,
    request: Request,
    _: bool = Depends(verify_task_authorization),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Manually trigger a completion check for a specific order.
    This can be used to fix stuck orders or test completion logic.
    """
    log.info(f"[Manual Trigger] Manually triggering completion check for order_id: {order_id}")
    
    result = await check_order_completion_immediately(order_id, supabase, cloud_tasks_service)
    
    return {
        "status": "success",
        "message": "Completion check triggered",
        "order_id": order_id,
        "result": result
    }

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
                    "monitoring_task_id": None,
                    "generation_task_id": None
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
                "monitoring_task_id": None,
                "generation_task_id": None
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
                next_check_delay = min(300, 30)  # Start with 30 seconds, max 5 minutes
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