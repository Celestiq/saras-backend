# app/api/routes/orchestrator.py
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

router = APIRouter(prefix="/tasks", tags=["orchestrator"])

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

def verify_orchestrator_authorization(authorization: str = Header(None)) -> bool:
    """
    Verify that the request is authorized by validating the Google-signed OIDC token
    specifically for the orchestrator endpoint.
    """
    if not authorization:
        log.warning("Orchestrator endpoint called without authorization header")
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
        log.warning("Orchestrator endpoint called with invalid authorization format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format"
        )

    try:
        # Define the expected audience for the orchestrator endpoint
        expected_audience = f"{settings.BACKEND_URL}/tasks/generate-chapters"
        
        log.debug(f"Attempting to verify orchestrator token with audience: {expected_audience}")
        id_info = id_token.verify_oauth2_token(
            token, requests.Request(), audience=expected_audience
        )
        log.info(f"Orchestrator token verified successfully with audience: {expected_audience}")
        
        # Optional: You can log the verified email for audit purposes
        log.info(f"Orchestrator task token verified for service account: {id_info.get('email')}")

    except ValueError as e:
        # This will catch invalid tokens, expired tokens, or audience mismatches
        log.error(f"Invalid orchestrator OIDC token: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token"
        )
    
    return True

# --- Helper Functions ---

async def process_chapter_generation_task(
    payload: ChapterGenerationTaskPayload,
    chapter_service: ChapterService,
    cloud_tasks_service: CloudTasksService,
    supabase: Client
):
    """
    Process the chapter generation task. This replaces the trigger_chapter_generation function.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0

    try:
        log.info(f"[Cloud Task] Fetching order details for order_id: {order_id}")
        order_response = supabase.table("orders").select("status, user_id, order_items(*)").eq("id", order_id).single().execute()

        if not order_response.data:
            log.error(f"[Cloud Task] Order not found for order_id: {order_id}. Acknowledging task to prevent retries.")
            # Return successfully to remove the task from the queue
            return
        
        order_data = order_response.data
        
        # If the order is already being processed or is complete, stop here.
        if order_data.get("status") in ["generating", "completed", "failed"]:
            log.warning(
                f"[Cloud Task] Order {order_id} is already in status '{order_data.get('status')}'. "
                f"Skipping duplicate task."
            )
            return

    except Exception as e:
        log.error(f"[Cloud Task] Failed to fetch or check status for order_id: {order_id}. Error: {e}", exc_info=True)
        # We raise an exception here so Cloud Tasks will retry, as we don't know the order state.
        raise

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
    
    log.info(f"[Cloud Task] Starting chapter task creation for order_id: {order_id} with {len(order_items)} item(s).")
    
    # Create individual chapter generation tasks for each book
    for item in order_items:
        book_id = item.get("book_id")
        if not book_id:
            log.warning(f"[Cloud Task] Skipping item with no book_id in order {order_id}.")
            continue
        
        try:   
            log.info(f"[Cloud Task] Creating individual chapter tasks for book_id: {book_id} from order {order_id}.")
            
            # Load book and roadmap to determine chapters to create
            book_response = supabase.table("books").select("content_url").eq("id", book_id).single().execute()
            log.debug(f"[Cloud Task] Book response for book_id: {book_id}: {book_response} and data: {book_response.data}")
            if not book_response.data:
                log.error(f"[Cloud Task] Book not found for book_id: {book_id}")
                failures_detected = True
                continue
            
            # Download and parse the roadmap
            roadmap_path = book_response.data["content_url"].replace("books/", "", 1)
            try:
                log.debug(f"[Cloud Task] Loading roadmap for book_id: {book_id} from path: {roadmap_path}")
                roadmap_raw = supabase.storage.from_("books").download(roadmap_path)
                roadmap = json.loads(roadmap_raw.decode("utf-8"))
            except Exception as e:
                log.error(f"[Cloud Task] Failed to load roadmap for book_id: {book_id}. Error: {e}", exc_info=True)
                failures_detected = True
                continue
            
            # Create individual tasks for each chapter
            chapter_idx = 1
            created_tasks = []
            
            for module_index, module in enumerate(roadmap.get("modules", [])):
                for topic_index, topic in enumerate(module.get("topics", [])):
                    chapter_title = topic.get("title", f"Chapter {chapter_idx}")
                    
                    try:
                        # Create a Cloud Task for this specific chapter
                        task_name = cloud_tasks_service.create_single_chapter_generation_task(
                            order_id=order_id,
                            book_id=book_id,
                            user_id=user_id,
                            chapter_idx=chapter_idx,
                            module_index=module_index,
                            topic_index=topic_index,
                            chapter_title=chapter_title
                        )
                        created_tasks.append(task_name)
                        log.info(f"[Cloud Task] Created chapter task {task_name} for book_id: {book_id}, chapter_idx: {chapter_idx}")
                        
                    except Exception as task_error:
                        log.error(f"[Cloud Task] Failed to create chapter task for book_id: {book_id}, chapter_idx: {chapter_idx}. Error: {task_error}", exc_info=True)
                        failures_detected = True
                    
                    chapter_idx += 1
            
            log.info(f"[Cloud Task] Successfully created {len(created_tasks)} chapter tasks for book_id: {book_id}")
                
        except Exception as e:
            log.error(f"[Cloud Task] Failed to create chapter tasks for book_id: {book_id} in order {order_id}. Error: {e}", exc_info=True)
            failures_detected = True

    # Since we're now creating individual chapter tasks instead of processing immediately,
    # we keep the order in "generating" status. Individual chapters will update book/order status as they complete.
    if failures_detected:
        # If we failed to create some tasks, mark the order as failed
        final_status = "failed"
        try:
            supabase.table("orders").update({"status": final_status}).eq("id", order_id).execute()
            log.info(f"[Cloud Task] Updated order {order_id} to status '{final_status}' due to task creation failures.")
        except Exception as e:
            log.error(f"[Cloud Task] CRITICAL: Failed to update order status to failed for order {order_id}. Error: {e}", exc_info=True)
    else:
        log.info(f"[Cloud Task] All chapter tasks created successfully for order {order_id}. Order remains in 'generating' status.")

    log.info(f"[Cloud Task] Finished creating chapter tasks for order_id: {order_id}.")

# --- API Routes ---

@router.post("/generate-chapters", summary="Cloud Tasks handler for orchestrating chapter generation")
async def handle_chapter_generation_task(
    payload: ChapterGenerationTaskPayload,
    _: bool = Depends(verify_orchestrator_authorization),
    chapter_service: ChapterService = Depends(get_chapter_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Handle chapter generation orchestration tasks from Google Cloud Tasks.
    This endpoint is called by Cloud Tasks to orchestrate chapter generation jobs by creating individual chapter tasks.
    """
    order_id = payload.order_id
    retry_count = payload.retry_count or 0
    max_retries = 3
    
    log.info(f"[Orchestrator Task Handler] Received chapter generation task for order_id: {order_id} (retry: {retry_count})")
    
    try:
        # Process the chapter generation
        await process_chapter_generation_task(
            payload=payload,
            chapter_service=chapter_service,
            cloud_tasks_service=cloud_tasks_service,
            supabase=supabase
        )
        
        log.info(f"[Orchestrator Task Handler] Successfully completed chapter generation orchestration for order_id: {order_id}")
        return {
            "status": "success",
            "message": f"Chapter generation orchestration completed for order {order_id}",
            "order_id": order_id,
            "retry_count": retry_count
        }
        
    except Exception as e:
        log.error(f"[Orchestrator Task Handler] Chapter generation orchestration failed for order_id: {order_id} (retry: {retry_count}). Error: {e}", exc_info=True)
        
        # If we haven't exceeded max retries, create a retry task
        if retry_count < max_retries:
            try:
                retry_task_name = cloud_tasks_service.create_retry_task(
                    order_id=order_id,
                    retry_count=retry_count + 1,
                    delay_seconds=300 * (retry_count + 1)  # Exponential backoff: 5, 10, 15 minutes
                )
                log.info(f"[Orchestrator Task Handler] Created retry task {retry_task_name} for order_id: {order_id}")
                
                return {
                    "status": "retry_scheduled",
                    "message": f"Task failed, retry scheduled (attempt {retry_count + 1}/{max_retries})",
                    "order_id": order_id,
                    "retry_count": retry_count + 1,
                    "retry_task_name": retry_task_name
                }
            except Exception as retry_error:
                log.error(f"[Orchestrator Task Handler] Failed to create retry task for order_id: {order_id}. Error: {retry_error}", exc_info=True)
        
        # Mark order as failed if we've exceeded retries or can't create retry task
        try:
            supabase.table("orders").update({"status": "failed"}).eq("id", order_id).execute()
            log.info(f"[Orchestrator Task Handler] Marked order {order_id} as failed after {retry_count + 1} attempts")
        except Exception as update_error:
            log.error(f"[Orchestrator Task Handler] Failed to mark order {order_id} as failed. Error: {update_error}", exc_info=True)
        
        # Return error response
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chapter generation orchestration failed for order {order_id} after {retry_count + 1} attempts"
        )
