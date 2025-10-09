# app/api/processors/task_processors.py
from supabase import Client
from datetime import datetime, timezone
from app.core.logging import get_logger
from app.services.chapter_service import ChapterService
from app.services.cloud_tasks_service import CloudTasksService
from app.api.helpers.task_helpers import handle_non_subscription_completion

log = get_logger(__name__)

async def process_chapter_generation_task(
    payload,
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
    
    # Filter out already completed books (book-level duplicate prevention)
    books_to_process = []
    for item in order_items:
        book_id = item.get("book_id")
        if not book_id:
            log.warning(f"[Cloud Task] Skipping item with no book_id in order {order_id}.")
            continue
        
        # Check if book is already completed
        try:
            book_check = supabase.table("books").select("status").eq("id", book_id).single().execute()
            if book_check.data:
                book_status = book_check.data.get("status")
                if book_status == "completed":
                    log.info(f"[Cloud Task] BOOK-LEVEL DUPLICATE PREVENTION: Book {book_id} is already completed. Skipping generation.")
                    continue
                else:
                    log.info(f"[Cloud Task] Book {book_id} status is '{book_status}', will proceed with generation.")
        except Exception as status_check_error:
            log.warning(f"[Cloud Task] Failed to check book status for book_id: {book_id}. Error: {status_check_error}. Proceeding with generation.")
        
        books_to_process.append(item)
    
    if not books_to_process:
        log.info(f"[Cloud Task] All books in order {order_id} are already completed. No generation needed.")
        # Update order status to completed since all books are done
        try:
            supabase.table("orders").update({"status": "completed", "generation_task_id": None}).eq("id", order_id).execute()
        except Exception as e:
            log.error(f"[Cloud Task] Failed to update order {order_id} status to 'completed'. Error: {e}", exc_info=True)
        return
    
    # Update only non-completed books to 'generating' status
    try:
        log.info(f"[Cloud Task] Updating {len(books_to_process)} book(s) in order {order_id} to 'generating' status.")
        chapter_service.update_books_status_for_order(books_to_process, "generating")
    except Exception as e:
        log.error(f"[Cloud Task] Failed to update books status to 'generating' for order {order_id}. Error: {e}", exc_info=True)
    
    log.info(f"[Cloud Task] Starting parallel chapter preparation for order_id: {order_id} with {len(books_to_process)} item(s).")
    
    total_tasks_created = 0
    
    # Phase 1: Prepare all chapters in database and create individual generation tasks
    for item in books_to_process:
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
                    # Use the enhanced duplicate prevention in create_individual_chapter_task
                    task_name = cloud_tasks_service.create_individual_chapter_task(
                        chapter_id=task_info["chapter_id"],
                        order_id=task_info["order_id"],
                        book_id=task_info["book_id"],
                        user_id=task_info["user_id"],
                        module_index=task_info["module_index"],
                        topic_index=task_info["topic_index"],
                        idx=task_info["idx"]
                    )
                    
                    # Check if task creation was skipped due to duplicate prevention
                    if task_name.startswith("already_completed_"):
                        log.info(f"[Cloud Task] Chapter {task_info['idx']} (ID: {task_info['chapter_id']}) already completed, skipped task creation")
                        continue
                    elif task_name.startswith("projects/"):
                        # Valid Cloud Task ID was created
                        total_tasks_created += 1
                        log.info(f"[Cloud Task] Created individual chapter task: {task_name} for chapter {task_info['idx']} (ID: {task_info['chapter_id']})")
                    else:
                        # Existing task ID was returned (duplicate prevention)
                        log.info(f"[Cloud Task] Chapter {task_info['idx']} (ID: {task_info['chapter_id']}) already being processed, using existing task: {task_name}")
                        total_tasks_created += 1
                    
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
                supabase.table("orders").update({"status": "failed", "error_message": "Failed to create completion monitoring", "generation_task_id": None}).eq("id", order_id).execute()
            except Exception as fallback_error:
                log.error(f"[Cloud Task] Failed to update order status as fallback. Error: {fallback_error}", exc_info=True)
    else:
        # No tasks were created, mark order as failed
        log.error(f"[Cloud Task] No chapter generation tasks were created for order {order_id}.")
        try:
            supabase.table("orders").update({"status": "failed", "error_message": "No chapters could be prepared for generation", "generation_task_id": None}).eq("id", order_id).execute()
        except Exception as e:
            log.error(f"[Cloud Task] Failed to mark order as failed. Error: {e}", exc_info=True)

    log.info(f"[Cloud Task] Finished setting up parallel chapter generation for order_id: {order_id}.")

async def process_chapter_generation_task_legacy(
    payload,
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
        log.info(f"[Cloud Task Legacy] Updating order {order_id} status to 'generating'.")
        supabase.table("orders").update({"status": "generating"}).eq("id", order_id).execute()
    except Exception as e:
        log.error(f"[Cloud Task Legacy] Failed to update order {order_id} status to 'generating'. Error: {e}", exc_info=True)
    
    # Filter out already completed books (book-level duplicate prevention)
    books_to_process = []
    for item in order_items:
        book_id = item.get("book_id")
        if not book_id:
            log.warning(f"[Cloud Task Legacy] Skipping item with no book_id in order {order_id}.")
            continue
        
        # Check if book is already completed
        try:
            book_check = supabase.table("books").select("status").eq("id", book_id).single().execute()
            if book_check.data:
                book_status = book_check.data.get("status")
                if book_status == "completed":
                    log.info(f"[Cloud Task Legacy] BOOK-LEVEL DUPLICATE PREVENTION: Book {book_id} is already completed. Skipping generation.")
                    continue
                else:
                    log.info(f"[Cloud Task Legacy] Book {book_id} status is '{book_status}', will proceed with generation.")
        except Exception as status_check_error:
            log.warning(f"[Cloud Task Legacy] Failed to check book status for book_id: {book_id}. Error: {status_check_error}. Proceeding with generation.")
        
        books_to_process.append(item)
    
    if not books_to_process:
        log.info(f"[Cloud Task Legacy] All books in order {order_id} are already completed. No generation needed.")
        # Update order status to completed since all books are done
        try:
            supabase.table("orders").update({"status": "completed", "generation_task_id": None}).eq("id", order_id).execute()
        except Exception as e:
            log.error(f"[Cloud Task Legacy] Failed to update order {order_id} status to 'completed'. Error: {e}", exc_info=True)
        return
    
    # Update only non-completed books to 'generating' status
    try:
        log.info(f"[Cloud Task Legacy] Updating {len(books_to_process)} book(s) in order {order_id} to 'generating' status.")
        chapter_service.update_books_status_for_order(books_to_process, "generating")
    except Exception as e:
        log.error(f"[Cloud Task Legacy] Failed to update books status to 'generating' for order {order_id}. Error: {e}", exc_info=True)
    
    log.info(f"[Cloud Task] Starting sequential chapter generation for order_id: {order_id} with {len(books_to_process)} item(s).")
    
    for item in books_to_process:
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
