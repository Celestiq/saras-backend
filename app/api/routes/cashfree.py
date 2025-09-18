# app/api/routes/cashfree.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from typing import Dict, Any

from app.db.supabase import get_supabase
from app.services.cart_service import CartService
from app.services.cashfree_service import CashfreeService
from app.services.user_service import UserService
from app.services.chapter_service import ChapterService
from app.api.deps import current_user
from app.core.logging import get_logger
from app.core.config import settings
from supabase import Client

# Import the background task function from cart.py
from app.api.routes.cart import trigger_chapter_generation_sync

log = get_logger(__name__)

router = APIRouter(prefix="/cashfree", tags=["cashfree"])

# --- Dependencies ---
def get_cart_service(supabase: Client = Depends(get_supabase)) -> CartService:
    """Dependency to provide a CartService instance."""
    return CartService(supabase=supabase)

def get_cashfree_service() -> CashfreeService:
    """Dependency to provide a CashfreeService instance."""
    return CashfreeService()

def get_user_service(supabase: Client = Depends(get_supabase)) -> UserService:
    """Dependency to provide a UserService instance."""
    return UserService(supabase=supabase)

def get_chapter_service(
    supabase: Client = Depends(get_supabase),
    openai_client = None  # We'll import OpenAI in the function to avoid circular imports
) -> ChapterService:
    """Dependency to provide a ChapterService instance."""
    from openai import OpenAI
    if openai_client is None:
        openai_client = OpenAI()
    return ChapterService(supabase=supabase, openai_client=openai_client)

# --- Cashfree Payment Endpoints ---

@router.post("/create-payment-session", summary="Create a Cashfree payment session based on cart contents")
async def create_payment_session(
    request: Request,
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service),
    cashfree_service: CashfreeService = Depends(get_cashfree_service),
    user_service: UserService = Depends(get_user_service)
):
    """
    Creates a Cashfree payment session for the user's cart contents.
    Since we're only implementing one-time payments, all cart items are treated as one-time purchases.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} creating Cashfree payment session")
    
    try:
        cart = cart_service.get_cart(user_id=user_id)
        if not cart or not cart.get("items"):
            raise HTTPException(status_code=400, detail="Cart is empty")
        
        # Create direct order record
        # cart_service.sb.table("direct_orders").insert({
        #     "order_id": cart["id"]
        # }).execute()
        
        # Calculate total amount for all cart items
        total_amount = cashfree_service.calculate_cart_totals(cart["items"])
        
        log.info(f"Cart analysis for user {user_id}: Total Amount = ${total_amount}")
        
        # Get user profile for customer details
        user_profile = user_service.get_user_profile(user_id)
        user_email = user_profile.get("email", "")
        user_name = user_profile.get("full_name", "User")
        
        # Prepare customer details for Cashfree
        customer_details = {
            "customer_id": user_id,
            "customer_name": user_name,
            "customer_email": user_email,
            "customer_phone": "9999999999"  # Placeholder phone number
        }
        
        # Set up return URL
        base_url = settings.FRONTEND_URL
        return_url = f"{base_url}/payment/success?gateway=cashfree"
        
        # Create Cashfree order
        log.info(f"Initiating Cashfree order creation. Total: ${total_amount}")
        order_id, payment_session_id = await cashfree_service.create_order(
            order_amount=total_amount,
            customer_details=customer_details,
            return_url=return_url
        )
        
        # Store Cashfree order information
        cart_service.store_cashfree_order_info(
            user_id=user_id,
            order_id=order_id,
            payment_session_id=payment_session_id
        )
        
        return {
            "payment_type": "order",
            "order_id": order_id,
            "payment_session_id": payment_session_id,
            "amount": total_amount,
            "currency": "INR"
        }
        
    except Exception as e:
        log.error(f"Error during Cashfree payment session creation for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create Cashfree payment session")

@router.post("/verify-payment/{order_id}", summary="Verify Cashfree payment status")
async def verify_cashfree_payment(
    order_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
    cashfree_service: CashfreeService = Depends(get_cashfree_service),
    cart_service: CartService = Depends(get_cart_service),
    user_service: UserService = Depends(get_user_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Verify the status of a Cashfree payment after user completes payment.
    """
    user_id = user.get("id")
    
    log.info(f"User {user_id} verifying Cashfree payment for order {order_id}")
    
    # Fetch user details from user service
    user_profile = user_service.get_user_profile(user_id)
    if not user_profile:
        log.error(f"User profile not found for user_id: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found"
        )
    
    user_name = user_profile.get("full_name", "User")
    log.info(f"Verifying payment for user: {user_name}")
    
    try:
        # Verify payment with Cashfree
        order_data = await cashfree_service.verify_order(order_id)
        order_status = order_data.get("order_status")
        log.debug(f"Cashfree order {order_id} status: {order_status}")
        
        if order_status == "PAID":
            # Payment is successful, update order status
            log.info(f"Cashfree payment {order_id} is successful, updating order status")
            
            # Update Cashfree order status in database
            cart_service.update_cashfree_order_status(
                user_id=user_id,
                order_id=order_id,
                status="completed"
            )
            
            # Get the order by Cashfree order ID
            order = cart_service.get_order_by_cashfree_order_id(order_id)
            if not order:
                log.error(f"No order found for Cashfree order {order_id}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Order not found for this payment"
                )
            
            db_order_id = order["order_id"]
            log.info(f"Updating order {db_order_id} status to 'pending' for content generation")

            # Update order status to 'pending' for content generation
            cart_service.sb.table("orders").update({
                "status": "pending",
                "time_to_send": "07:00"
            }).eq("id", db_order_id).execute()
            
            # Get the updated order with items
            updated_order = cart_service.sb.table("orders").select("*, order_items(*), direct_orders(*)").eq("id", db_order_id).single().execute()
            
            log.info(f"Successfully updated order {db_order_id} to 'pending' status")
            
            # Trigger background task for content generation
            background_tasks.add_task(
                trigger_chapter_generation_sync,
                order=updated_order.data,
                chapter_service=chapter_service,
                supabase=supabase
            )
            log.info("Went through background task addition")
            log.info(f"Enqueued chapter generation task for order_id: {db_order_id}")
            
            return {
                "status": "success",
                "message": "Payment verified and order processed successfully. Content generation has started.",
                "order": updated_order.data,
                "payment_details": order_data
            }
        else:
            log.warning(f"Cashfree payment {order_id} status is {order_status}")
            
            # Update payment status even if not paid yet
            cart_service.update_cashfree_order_status(
                user_id=user_id,
                order_id=order_id,
                status=order_status.lower()
            )
            
            return {
                "status": "active",
                "message": f"Payment status: {order_status}",
                "payment_details": order_data
            }
            
    except HTTPException as e:
        log.error(f"HTTP error verifying Cashfree payment for user {user_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error verifying Cashfree payment for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify Cashfree payment"
        )

@router.get("/order-status/{order_id}", summary="Get Cashfree order status")
async def get_cashfree_order_status(
    order_id: str,
    user: dict = Depends(current_user),
    cashfree_service: CashfreeService = Depends(get_cashfree_service)
):
    """
    Get the current status of a Cashfree order.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} checking status for Cashfree order {order_id}")
    
    try:
        order_data = await cashfree_service.verify_order(order_id)
        
        return {
            "order_id": order_id,
            "status": order_data.get("order_status"),
            "amount": order_data.get("order_amount"),
            "currency": order_data.get("order_currency"),
            "customer_details": order_data.get("customer_details"),
            "created_at": order_data.get("created_at"),
            "order_expiry_time": order_data.get("order_expiry_time")
        }
        
    except Exception as e:
        log.error(f"Error checking Cashfree order status for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to check order status")
