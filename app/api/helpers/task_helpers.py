# app/api/helpers/task_helpers.py
from supabase import Client
from datetime import datetime, timezone
from app.core.logging import get_logger
from app.services.cloud_tasks_service import CloudTasksService

log = get_logger(__name__)

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
        
        # Check if PDF generation has already been triggered for this book (idempotency check)
        try:
            # Check if there's already a PDF generation task or completed PDF for this book
            existing_pdf_response = supabase.table("books").select("pdf_generation_task_id, pdf_url").eq("id", book_id).execute()
            
            if existing_pdf_response.data and len(existing_pdf_response.data) > 0:
                book_data = existing_pdf_response.data[0]
                pdf_task_id = book_data.get("pdf_generation_task_id")
                pdf_url = book_data.get("pdf_url")
                
                if pdf_url:
                    log.info(f"[Cloud Task] PDF already exists for book_id: {book_id} with URL: {pdf_url}. Skipping PDF generation.")
                    return
                elif pdf_task_id:
                    # Check if the task ID is a temporary claim that might be stale
                    if pdf_task_id.startswith("task_"):
                        try:
                            # Extract timestamp from task ID (format: task_timestamp_bookid)
                            timestamp_str = pdf_task_id.split("_")[1]
                            claim_time = float(timestamp_str)
                            current_time = datetime.now(timezone.utc).timestamp()
                            
                            # If claim is older than 5 minutes, consider it stale and retry
                            if current_time - claim_time > 300:  # 5 minutes
                                log.warning(f"[Cloud Task] Stale PDF generation claim for book_id: {book_id} (age: {current_time - claim_time:.1f}s). Retrying.")
                                # Clear the stale claim and proceed
                                supabase.table("books").update({
                                    "pdf_generation_task_id": None,
                                    "updated_at": datetime.now(timezone.utc).isoformat()
                                }).eq("id", book_id).execute()
                            else:
                                log.info(f"[Cloud Task] PDF generation task already exists for book_id: {book_id} with task_id: {pdf_task_id}. Skipping duplicate task creation.")
                                return
                        except (ValueError, IndexError) as e:
                            log.warning(f"[Cloud Task] Invalid task ID format for book_id: {book_id}: {pdf_task_id}. Clearing and retrying.")
                            # Clear invalid task ID and proceed
                            supabase.table("books").update({
                                "pdf_generation_task_id": None,
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            }).eq("id", book_id).execute()
                    else:
                        log.info(f"[Cloud Task] PDF generation task already exists for book_id: {book_id} with task_id: {pdf_task_id}. Skipping duplicate task creation.")
                        return
                    
        except Exception as check_error:
            log.warning(f"[Cloud Task] Error checking existing PDF for book_id: {book_id}. Error: {check_error}. Proceeding with PDF generation.")
        
        # Create PDF generation task instead of synchronous PDF generation
        try:
            from app.services.cloud_tasks_service import CloudTasksService
            cloud_tasks_service = CloudTasksService()
            
            pdf_task_name = cloud_tasks_service.create_pdf_generation_task(
                order_id=order_id,
                book_id=book_id,
                user_id=user_id,
                book_title=book_title,
                delay_seconds=5  # Small delay to ensure all chapters are fully processed
            )
            log.info(f"[Cloud Task] Created PDF generation task: {pdf_task_name} for book_id: {book_id}")
            
        except Exception as pdf_task_error:
            log.error(f"[Cloud Task] Error creating PDF generation task for book_id: {book_id}: {pdf_task_error}", exc_info=True)
        
    except Exception as e:
        log.error(f"[Cloud Task] Error in handle_non_subscription_completion for book_id: {book_id}. Error: {e}", exc_info=True)
        raise

async def check_order_completion_immediately(
    order_id: str,
    supabase: Client,
    cloud_tasks_service: CloudTasksService
) -> dict:
    """
    Immediately check if an order is complete and trigger final processing if so.
    This prevents timing gaps where the last chapter completes but the scheduled
    monitoring task hasn't run yet.
    """
    log.info(f"[Immediate Completion Check] Checking completion status for order_id: {order_id}")
    
    try:
        # Get all chapters for this order
        order_response = supabase.table("orders").select("*, order_items(*)").eq("id", order_id).single().execute()
        if not order_response.data:
            log.error(f"[Immediate Completion Check] Order not found: {order_id}")
            return {"status": "error", "message": "Order not found"}
        
        order_data = order_response.data
        order_items = order_data.get("order_items", [])
        user_id = order_data.get("user_id")
        
        if not order_items:
            log.warning(f"[Immediate Completion Check] No order items found for order_id: {order_id}")
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
            
            # Get book metadata including expected chapter count
            expected_chapters = None
            try:
                book_response = supabase.table("books").select("id, num_chapters").eq("id", book_id).single().execute()
                book_data = book_response.data
                expected_chapters = book_data.get("num_chapters") if book_data else None
            except Exception as book_error:
                log.warning(f"[Immediate Completion Check] Could not fetch book metadata for book_id: {book_id}. Error: {book_error}. Proceeding with chapter checks only.")
                expected_chapters = None
                
            # Get all chapters for this book
            chapters_response = supabase.table("chapters").select("id, content_path, status").eq("book_id", book_id).execute()
            book_chapters = chapters_response.data
            
            # Critical check: If no chapters exist yet, book is not complete
            if len(book_chapters) == 0:
                if expected_chapters is not None:
                    log.warning(f"[Immediate Completion Check] Book {book_id} has 0 chapters created (expected: {expected_chapters}). Marking as incomplete.")
                else:
                    log.warning(f"[Immediate Completion Check] Book {book_id} has 0 chapters created. Marking as incomplete.")
                all_completed = False
                continue
            
            # If we know the expected chapter count, verify it matches
            if expected_chapters is not None and len(book_chapters) < expected_chapters:
                log.warning(f"[Immediate Completion Check] Book {book_id} has {len(book_chapters)} chapters but expected {expected_chapters}. Marking as incomplete.")
                all_completed = False
                continue
            elif expected_chapters is None and len(book_chapters) > 0:
                # If we don't know the expected count but have some chapters, log it for visibility
                log.info(f"[Immediate Completion Check] Book {book_id} has {len(book_chapters)} chapters")
            
            # Check each chapter's completion status
            for chapter in book_chapters:
                total_chapters += 1
                if chapter.get("content_path"):  # Chapter has been generated
                    completed_chapters += 1
                elif chapter.get("status") == "failed":
                    failed_chapters += 1
                else:
                    all_completed = False
        
        log.info(f"[Immediate Completion Check] Order {order_id}: {completed_chapters}/{total_chapters} chapters completed, {failed_chapters} failed")
        
        if all_completed and total_chapters > 0:
            # All chapters are complete, trigger final processing
            log.info(f"[Immediate Completion Check] All chapters completed for order {order_id}, starting final processing")
            
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
                            log.info(f"[Immediate Completion Check] Updated book_id: {book_id} status to 'completed'")
                            
                            # Handle PDF generation and email for non-subscription books
                            await handle_non_subscription_completion(
                                order_id=order_id,
                                book_id=book_id,
                                user_id=user_id,
                                supabase=supabase
                            )
                        except Exception as book_error:
                            log.error(f"[Immediate Completion Check] Failed to process book_id: {book_id}. Error: {book_error}", exc_info=True)
                
                log.info(f"[Immediate Completion Check] Successfully completed final processing for order {order_id}")
                return {
                    "status": "completed",
                    "message": "All chapters completed, final processing done",
                    "order_id": order_id,
                    "total_chapters": total_chapters,
                    "completed_chapters": completed_chapters,
                    "failed_chapters": failed_chapters
                }
                
            except Exception as processing_error:
                log.error(f"[Immediate Completion Check] Failed final processing for order {order_id}. Error: {processing_error}", exc_info=True)
                # Mark order as failed and clear monitoring task ID
                supabase.table("orders").update({
                    "status": "failed",
                    "monitoring_task_id": None
                }).eq("id", order_id).execute()
                return {
                    "status": "error",
                    "message": "Final processing failed",
                    "error": str(processing_error)
                }
        
        else:
            # Not all chapters are complete yet
            log.info(f"[Immediate Completion Check] Order {order_id} not yet complete ({completed_chapters}/{total_chapters} chapters done)")
            return {
                "status": "in_progress",
                "message": "Order not yet complete",
                "order_id": order_id,
                "total_chapters": total_chapters,
                "completed_chapters": completed_chapters,
                "failed_chapters": failed_chapters
            }
            
    except Exception as e:
        log.error(f"[Immediate Completion Check] Error checking completion for order {order_id}. Error: {e}", exc_info=True)
        return {
            "status": "error",
            "message": "Error checking completion",
            "error": str(e)
        }
