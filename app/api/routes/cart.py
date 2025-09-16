# app/api/routes/cart.py
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from supabase import Client
from openai import OpenAI

from app.db.supabase import get_supabase
from app.services.cart_service import CartService
from app.services.chapter_service import ChapterService
from app.services.pdf_service import PDFService
from app.services.email_service import EmailService
from app.domain.models import CartItemAdd, CartItemUpdate, CheckoutRequest, CartResponse
from app.api.deps import current_user
from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/cart", tags=["cart"])

# --- Dependencies ---
def get_cart_service(supabase: Client = Depends(get_supabase)) -> CartService:
    """Dependency to provide a CartService instance."""
    return CartService(supabase=supabase)

def get_openai_client() -> OpenAI:
    """Dependency to provide an OpenAI client instance."""
    return OpenAI()

def get_chapter_service(
    supabase: Client = Depends(get_supabase),
    openai_client: OpenAI = Depends(get_openai_client)
) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    return ChapterService(supabase=supabase, openai_client=openai_client)

# --- Helper Functions ---

async def handle_non_subscription_completion(
    order_id: str,
    book_id: str, 
    user_id: str,
    supabase: Client
):
    """
    Handles PDF generation and email sending for non-subscription books.
    """
    try:
        # Check if this book is a subscription in the order_items table
        try:
            order_item_response = supabase.table("order_items").select("subscription").eq("order_id", order_id).eq("book_id", book_id).execute()
            
            if not order_item_response.data or len(order_item_response.data) == 0:
                log.warning(f"[BG Task] No order item found for book_id: {book_id} in order: {order_id}")
                return
                
            is_subscription = order_item_response.data[0].get("subscription", True)
            log.info(f"[BG Task] Order item subscription status for book_id: {book_id} is: {is_subscription}")
            
        except Exception as e:
            log.error(f"[BG Task] Error fetching order item for book_id: {book_id}, order_id: {order_id}. Error: {e}", exc_info=True)
            return
        
        if is_subscription:
            log.info(f"[BG Task] Book_id: {book_id} is a subscription, skipping PDF generation and email.")
            return
            
        log.info(f"[BG Task] Book_id: {book_id} is not a subscription, proceeding with PDF generation and email.")
        
        # Get user email from profiles table with fallback to auth.users
        user_email = None
        
        try:
            # First try to get email from profiles table
            profile_response = supabase.table("profiles").select("email").eq("user_id", user_id).execute()
            
            if profile_response.data and len(profile_response.data) > 0 and profile_response.data[0].get("email"):
                user_email = profile_response.data[0]["email"]
                log.info(f"[BG Task] Found email in profiles table for user_id: {user_id}")
            else:
                log.warning(f"[BG Task] No email found in profiles table for user_id: {user_id}, trying auth.users")
                
                # Fallback to auth.users table
                auth_response = supabase.auth.admin.get_user_by_id(user_id)
                if auth_response and hasattr(auth_response, 'user') and auth_response.user and auth_response.user.email:
                    user_email = auth_response.user.email
                    log.info(f"[BG Task] Found email in auth.users table for user_id: {user_id}")
                else:
                    log.error(f"[BG Task] No email found in either profiles or auth.users for user_id: {user_id}")
                    return
                    
        except Exception as e:
            log.error(f"[BG Task] Error fetching user email for user_id: {user_id}. Error: {e}", exc_info=True)
            return
            
        if not user_email:
            log.error(f"[BG Task] No email found for user_id: {user_id}")
            return
        
        # Get book title for PDF generation
        book_title = "SARAS eBook"  # Default title
        try:
            book_response = supabase.table("books").select("generated_title").eq("id", book_id).execute()
            if book_response.data and len(book_response.data) > 0 and book_response.data[0].get("generated_title"):
                book_title = book_response.data[0]["generated_title"]
                log.info(f"[BG Task] Found book title: {book_title} for book_id: {book_id}")
            else:
                log.warning(f"[BG Task] No book title found for book_id: {book_id}, using default")
        except Exception as e:
            log.error(f"[BG Task] Error fetching book title for book_id: {book_id}. Error: {e}", exc_info=True)
            # Continue with default title
        
        # Generate PDF using the PDF service directly
        try:
            pdf_service = PDFService(supabase)
            pdf_result = pdf_service.create_book_pdf(
                book_id=book_id,
                book_title=book_title,
                subscription=False
            )
            log.info(f"[BG Task] PDF generated successfully for book_id: {book_id}. PDF URL: {pdf_result.get('storage_url')}")
        except Exception as e:
            log.error(f"[BG Task] PDF generation failed for book_id: {book_id}. Error: {e}", exc_info=True)
            return
        
        # Send email with PDF attachment using the email service directly
        try:
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
            log.info(f"[BG Task] Email sent successfully to {user_email} for book_id: {book_id}")
        except Exception as e:
            log.error(f"[BG Task] Email sending failed for book_id: {book_id}. Error: {e}", exc_info=True)
            return
        
    except Exception as e:
        log.error(f"[BG Task] Error in handle_non_subscription_completion for book_id: {book_id}. Error: {e}", exc_info=True)
        raise

# --- Worker Function for Background Task ---

def trigger_chapter_generation_sync(
    order: dict,
    chapter_service: ChapterService,
    supabase: Client
):
    """
    Synchronous wrapper for the async trigger_chapter_generation function.
    """
    import asyncio
    asyncio.run(trigger_chapter_generation(order, chapter_service, supabase))

async def trigger_chapter_generation(
    order: dict,
    chapter_service: ChapterService,
    supabase: Client
):
    """
    This function is executed in the background after the checkout response is sent.
    It iterates through the items of a completed order and generates all chapters.
    """
    order_id = order.get("id")
    user_id = order.get("user_id")
    order_items = order.get("order_items", [])
    failures_detected = False

    try:
        log.info(f"[BG Task] Updating order {order_id} status to 'generating'.")
        supabase.table("orders").update({"status": "generating"}).eq("id", order_id).execute()
    except Exception as e:
        log.error(f"[BG Task] Failed to update order {order_id} status to 'generating'. Error: {e}", exc_info=True)
    
    # Update all books to 'generating' status
    try:
        log.info(f"[BG Task] Updating all books in order {order_id} to 'generating' status.")
        chapter_service.update_books_status_for_order(order_items, "generating")
    except Exception as e:
        log.error(f"[BG Task] Failed to update books status to 'generating' for order {order_id}. Error: {e}", exc_info=True)
    
    log.info(f"[BG Task] Starting chapter generation for order_id: {order_id} with {len(order_items)} item(s).")
    
    for item in order_items:
        book_id = item.get("book_id")
        if not book_id:
            log.warning(f"[BG Task] Skipping item with no book_id in order {order_id}.")
            continue
        
        try:
            log.info(f"[BG Task] Generating all chapters for book_id: {book_id} from order {order_id}.")
            chapter_service.generate_all_chapters(
                user_id=user_id,
                wish_id=None,
                book_id=book_id
            )
            log.info(f"[BG Task] Successfully generated all chapters for book_id: {book_id}.")
            
            # Update this specific book to 'completed' status
            try:
                chapter_service.update_book_status(book_id, "completed")
                log.info(f"[BG Task] Successfully updated book_id: {book_id} status to 'completed'.")
                
                # Check if this is a non-subscription book and handle PDF generation + email
                try:
                    await handle_non_subscription_completion(
                        order_id=order_id,
                        book_id=book_id,
                        user_id=user_id,
                        supabase=supabase
                    )
                except Exception as pdf_email_error:
                    log.error(f"[BG Task] Failed to handle PDF generation/email for book_id: {book_id}. Error: {pdf_email_error}", exc_info=True)
                    # Don't mark the entire order as failed for PDF/email issues
                    
            except Exception as e:
                log.error(f"[BG Task] Failed to update book_id: {book_id} status to 'completed'. Error: {e}", exc_info=True)
                failures_detected = True
                
        except Exception as e:
            log.error(f"[BG Task] Failed to generate chapters for book_id: {book_id} in order {order_id}. Error: {e}", exc_info=True)
            failures_detected = True
            
            # Update this specific book to 'failed' status
            try:
                chapter_service.update_book_status(book_id, "failed")
                log.info(f"[BG Task] Updated book_id: {book_id} status to 'failed' due to generation error.")
            except Exception as update_error:
                log.error(f"[BG Task] Failed to update book_id: {book_id} status to 'failed'. Error: {update_error}", exc_info=True)

    final_status = "failed" if failures_detected else "completed"
    log.info(f"[BG Task] All generation tasks finished for order {order_id}. Setting final status to '{final_status}'.")
    try:
        supabase.table("orders").update({"status": final_status}).eq("id", order_id).execute()
        log.info(f"[BG Task] Successfully updated order {order_id} to status '{final_status}'.")
    except Exception as e:
        log.error(f"[BG Task] CRITICAL: Failed to update final status for order {order_id}. Error: {e}", exc_info=True)

    log.info(f"[BG Task] Finished processing order_id: {order_id}.")

# --- API Routes ---

@router.get("", response_model=CartResponse, summary="Get the current user's cart")
def get_cart_contents(
    user: dict = Depends(current_user),
    service: CartService = Depends(get_cart_service)
):
    user_id = user.get("id")
    log.info(f"Fetching cart for user_id: {user_id}")
    try:
        cart = service.get_cart(user_id=user_id)
        log.info(f"Successfully fetched cart for user_id: {user_id}")
        return cart
    except Exception as e:
        log.critical(f"An unexpected error occurred while fetching cart for user_id: {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while fetching cart.")


@router.post("/items", response_model=CartResponse, status_code=status.HTTP_201_CREATED, summary="Add an item to the cart")
def add_item(
    body: CartItemAdd,
    user: dict = Depends(current_user),
    service: CartService = Depends(get_cart_service)
):
    user_id = user.get("id")
    log.info(f"User {user_id} attempting to add book_id: {body.book_id} to cart.")
    try:
        cart = service.add_item_to_cart(
            user_id=user_id,
            book_id=body.book_id,
            subscription=body.subscription
        )
        log.info(f"Successfully added book_id: {body.book_id} to cart for user_id: {user_id}")
        return cart
    except HTTPException as e:
        log.error(f"HTTP error adding item to cart for user {user_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"Unexpected error adding item to cart for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while adding item to cart.")


@router.put("/items/{book_id}", response_model=CartResponse, summary="Update an item in the cart")
def update_item(
    book_id: str,
    body: CartItemUpdate,
    user: dict = Depends(current_user),
    service: CartService = Depends(get_cart_service)
):
    user_id = user.get("id")
    log.info(f"User {user_id} attempting to update book_id: {book_id} in cart.")
    try:
        cart = service.update_cart_item(
            user_id=user_id,
            book_id=book_id,
            subscription=body.subscription
        )
        log.info(f"Successfully updated book_id: {book_id} for user_id: {user_id}")
        return cart
    except HTTPException as e:
        log.error(f"HTTP error updating item for user {user_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"Unexpected error updating item for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while updating cart item.")


@router.delete("/items/{book_id}", response_model=CartResponse, summary="Remove an item from the cart")
def remove_item(
    book_id: str,
    user: dict = Depends(current_user),
    service: CartService = Depends(get_cart_service)
):
    user_id = user.get("id")
    log.info(f"User {user_id} attempting to remove book_id: {book_id} from cart.")
    try:
        cart = service.remove_item_from_cart(user_id=user_id, book_id=book_id)
        log.info(f"Successfully removed book_id: {book_id} from cart for user_id: {user_id}")
        return cart
    except Exception as e:
        log.critical(f"Unexpected error removing item from cart for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error while removing cart item.")


@router.post("/checkout", summary="Checkout and trigger content generation")
def checkout_cart(
    body: CheckoutRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Processes checkout, updates order status, and triggers a background task
    to generate all chapters for the purchased books.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} attempting to checkout.")
    try:
        # The service updates the order status and returns the finalized order
        final_order = cart_service.checkout(user_id=user_id, time_to_send=body.time_to_send)
        log.info(f"User {user_id} successfully checked out order_id: {final_order.get('id')}")

        # Add the long-running job to the background
        background_tasks.add_task(
            trigger_chapter_generation_sync,
            order=final_order,
            chapter_service=chapter_service,
            supabase=supabase
        )
        
        log.info(f"Enqueued chapter generation task for order_id: {final_order.get('id')}")

        return {"message": "Checkout successful. Content generation has started.", "order": final_order}
    except HTTPException as e:
        log.error(f"HTTP error during checkout for user {user_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"An unexpected error occurred during checkout for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred during checkout.")


@router.post("/checkout/credits", summary="Checkout using credits")
def checkout_with_credits(
    body: CheckoutRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Processes checkout using user credits if sufficient, otherwise raises an error.
    """
    user_id = user.get("id")
    log.info(f"[API_CREDIT_CHECKOUT] User {user_id} attempting credit-based checkout with time_to_send: {body.time_to_send}")
    try:
        # Use the credit-based checkout method
        log.debug(f"[API_CREDIT_CHECKOUT] Calling cart_service.checkout_with_credits for user_id: {user_id}")
        final_order = cart_service.checkout_with_credits(user_id=user_id, time_to_send=body.time_to_send)
        order_id = final_order.get('id')
        log.info(f"[API_CREDIT_CHECKOUT] User {user_id} successfully checked out with credits, order_id: {order_id}")

        # Add the long-running job to the background
        log.debug(f"[API_CREDIT_CHECKOUT] Enqueuing chapter generation task for order_id: {order_id}")
        background_tasks.add_task(
            trigger_chapter_generation_sync,
            order=final_order,
            chapter_service=chapter_service,
            supabase=supabase
        )
        
        log.info(f"[API_CREDIT_CHECKOUT] Successfully enqueued chapter generation task for order_id: {order_id}")

        return {"message": "Credit checkout successful. Content generation has started.", "order": final_order}
    except HTTPException as e:
        log.error(f"[API_CREDIT_CHECKOUT] HTTP error during credit checkout for user {user_id}. Status: {e.status_code}, Detail: {e.detail}")
        raise e
    except Exception as e:
        log.critical(f"[API_CREDIT_CHECKOUT] An unexpected error occurred during credit checkout for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred during credit checkout.")


@router.get("/check-credits", summary="Check if user has sufficient credits for checkout")
def check_credits(
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service)
):
    """
    Checks if the user has sufficient credits to checkout their current cart.
    """
    user_id = user.get("id")
    log.info(f"[API_CHECK_CREDITS] Checking credits for user_id: {user_id}")
    try:
        # Get current cart total
        log.debug(f"[API_CHECK_CREDITS] Fetching cart for user_id: {user_id}")
        cart = cart_service.get_cart(user_id=user_id)
        cart_total = cart.get("total", 0)
        log.debug(f"[API_CHECK_CREDITS] Cart total for user_id {user_id}: ${cart_total}")
        
        # Check user credits
        log.debug(f"[API_CHECK_CREDITS] Checking user credits for user_id: {user_id}")
        credit_info = cart_service.check_user_credits(user_id)
        
        sufficient_credits = credit_info["credits"] >= cart_total
        log.info(f"[API_CHECK_CREDITS] Credit check result for user_id {user_id}: {credit_info['credits']} credits, ${cart_total} needed, sufficient: {sufficient_credits}")
        
        return {
            "cart_total": cart_total,
            "user_credits": credit_info["credits"],
            "sufficient_credits": sufficient_credits,
            "has_profile": credit_info["has_profile"]
        }
    except Exception as e:
        log.critical(f"[API_CHECK_CREDITS] An unexpected error occurred while checking credits for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred while checking credits.")