# app/api/routes/cart.py
from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from openai import OpenAI

from app.db.supabase import get_supabase
from app.services.cart_service import CartService
from app.services.chapter_service import ChapterService
from app.services.email_service import EmailService
from app.services.user_service import UserService
from app.services.pdf_service import PDFService
from app.services.cloud_tasks_service import CloudTasksService
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

def get_email_service() -> EmailService:
    """Dependency to provide an EmailService instance."""
    return EmailService()

def get_user_service(supabase: Client = Depends(get_supabase)) -> UserService:
    """Dependency to provide a UserService instance."""
    return UserService(supabase=supabase)

def get_cloud_tasks_service() -> CloudTasksService:
    """Dependency to provide a CloudTasksService instance."""
    return CloudTasksService()

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
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Processes checkout, updates order status, and triggers a Cloud Task
    to generate all chapters for the purchased books.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} attempting to checkout.")
    try:
        # The service updates the order status and returns the finalized order
        final_order = cart_service.checkout(user_id=user_id, time_to_send=body.time_to_send)
        log.info(f"User {user_id} successfully checked out order_id: {final_order.get('id')}")

        # Create Cloud Task for chapter generation
        try:
            order_id = final_order.get('id')
            task_name = cloud_tasks_service.create_chapter_generation_task(order_id=order_id)
            log.info(f"Created Cloud Task for chapter generation: {task_name} for order_id: {order_id}")
        except Exception as task_error:
            log.error(f"Failed to create Cloud Task for order_id: {final_order.get('id')}. Error: {task_error}", exc_info=True)
            # Don't fail the checkout if task creation fails - the order is already processed
            # We could implement a fallback mechanism here if needed

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
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service),
    email_service: EmailService = Depends(get_email_service),
    cloud_tasks_service: CloudTasksService = Depends(get_cloud_tasks_service),
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

        # Send confirmation email
        try:
            email_service.send_confirmation_email(order_id)
            log.info(f"[API_CREDIT_CHECKOUT] Confirmation email sent successfully for order {order_id}")
        except Exception as email_error:
            log.error(f"[API_CREDIT_CHECKOUT] Failed to send confirmation email for order {order_id}: {email_error}")
            # Don't fail the whole process if email fails

        # Create Cloud Task for chapter generation
        log.debug(f"[API_CREDIT_CHECKOUT] Creating Cloud Task for chapter generation, order_id: {order_id}")
        try:
            task_name = cloud_tasks_service.create_chapter_generation_task(order_id=order_id)
            log.info(f"[API_CREDIT_CHECKOUT] Created Cloud Task for chapter generation: {task_name} for order_id: {order_id}")
        except Exception as task_error:
            log.error(f"[API_CREDIT_CHECKOUT] Failed to create Cloud Task for order_id: {order_id}. Error: {task_error}", exc_info=True)
            # Don't fail the checkout if task creation fails - the order is already processed

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