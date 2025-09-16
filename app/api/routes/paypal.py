# app/api/routes/paypal.py
from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from typing import Dict, Any
from urllib.parse import urlencode

from app.db.supabase import get_supabase
from app.services.cart_service import CartService
from app.services.paypal_service import PayPalService
from app.services.user_service import UserService
from app.services.chapter_service import ChapterService
from app.api.deps import current_user
from app.core.logging import get_logger
from app.core.config import settings
from supabase import Client

# Import the background task function from cart.py
from app.api.routes.cart import trigger_chapter_generation

log = get_logger(__name__)

router = APIRouter(prefix="/paypal", tags=["paypal"])

# --- Dependencies ---
def get_cart_service(supabase: Client = Depends(get_supabase)) -> CartService:
    """Dependency to provide a CartService instance."""
    return CartService(supabase=supabase)

def get_paypal_service() -> PayPalService:
    """Dependency to provide a PayPalService instance."""
    return PayPalService()

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

# --- PayPal Subscription Endpoints ---

@router.post("/create-payment-session", summary="Create a PayPal payment session based on cart contents")
async def create_payment_session(
    request: Request,
    user: dict = Depends(current_user),
    cart_service: CartService = Depends(get_cart_service),
    paypal_service: PayPalService = Depends(get_paypal_service),
    user_service: UserService = Depends(get_user_service)
):
    """
    Analyzes the user's cart and creates either a PayPal Subscription or a PayPal Order.
    """
    user_id = user.get("id")
    log.info(f"User {user_id} creating PayPal payment session")
    try:
        cart = cart_service.get_cart(user_id=user_id)
        if not cart or not cart.get("items"):
            raise HTTPException(status_code=400, detail="Cart is empty")
        cart_service.sb.table("direct_orders").insert({
            "order_id": cart["id"]
        }).execute()

        subscription_total, one_time_total = paypal_service.calculate_cart_totals(cart["items"])
        has_subscriptions = subscription_total > 0

        log.info(f"Cart analysis for user {user_id}: Subscription Total = ${subscription_total}, One-Time Total = ${one_time_total}, Has Subscriptions = {has_subscriptions}")

        user_profile = user_service.get_user_profile(user_id)
        user_email = user_profile.get("email", "")
        user_name = user_profile.get("full_name", "User")

        # In a real app, get base_url from config
        base_url = settings.FRONTEND_URL
        return_url = f"{base_url}/payment/success"
        cancel_url = f"{base_url}/payment/cancelled"

        if has_subscriptions:
            # --- SUBSCRIPTION FLOW ---
            log.info(f"Initiating SUBSCRIPTION flow. Sub Total: ${subscription_total}, Setup Fee: ${one_time_total}")
            plan_id = await paypal_service.create_billing_plan(
                subscription_price=subscription_total,
                setup_fee=one_time_total
            )
            subscription_id, approval_url = await paypal_service.create_subscription(
                plan_id=plan_id,
                subscriber_name=user_name,
                subscriber_email=user_email,
                return_url=return_url,
                cancel_url=cancel_url
            )
            cart_service.store_paypal_subscription_info(
                user_id=user_id,
                subscription_id=subscription_id,
                plan_id=plan_id
            )
            return {"payment_type": "subscription", "subscription_id": subscription_id, "approval_url": approval_url}
        else:
            # --- ONE-TIME ORDER FLOW ---
            log.info(f"Initiating ONE-TIME ORDER flow. Total: ${one_time_total}")
            order_id, approval_url = await paypal_service.create_order(
                total_amount=one_time_total,
                return_url=return_url,
                cancel_url=cancel_url
            )
            cart_service.store_paypal_order_info(user_id=user_id, order_id=order_id)
            return {"payment_type": "order", "order_id": order_id, "approval_url": approval_url}
    except Exception as e:
        log.error(f"Error during payment session creation for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create PayPal payment session")

@router.get("/verify-subscription/{subscription_id}", summary="Verify PayPal subscription status")
async def verify_paypal_subscription(
    subscription_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
    paypal_service: PayPalService = Depends(get_paypal_service),
    cart_service: CartService = Depends(get_cart_service),
    user_service: UserService = Depends(get_user_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
    supabase: Client = Depends(get_supabase)
):
    """
    Verify the status of a PayPal subscription after user approval.
    """
    user_id = user.get("id")
    
    log.info(f"User {user_id} verifying PayPal subscription {subscription_id}")
    
    # Fetch user details from user service
    user_profile = user_service.get_user_profile(user_id)
    if not user_profile:
        log.error(f"User profile not found for user_id: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found"
        )
    
    user_name = user_profile.get("full_name", "User")
    log.info(f"Verifying subscription for user: {user_name}")
    
    try:
        # Verify subscription with PayPal
        subscription_data = await paypal_service.verify_subscription(subscription_id)
        subscription_status = subscription_data.get("status")
        
        if subscription_status == "ACTIVE":
            # Subscription is active, update order status and PayPal subscription status
            log.info(f"PayPal subscription {subscription_id} is active, updating order status")
            
            # Update PayPal subscription status in database
            cart_service.update_paypal_subscription_status(
                user_id=user_id,
                subscription_id=subscription_id,
                status="active"
            )
            
            # Get the order by PayPal subscription ID
            order = cart_service.get_order_by_paypal_subscription_id(subscription_id)
            if not order:
                log.error(f"No order found for PayPal subscription {subscription_id}")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Order not found for this subscription"
                )
            
            order_id = order["id"]
            log.info(f"Updating order {order_id} status to 'pending' for content generation")
            
            # Update order status to 'pending' for content generation
            cart_service.sb.table("orders").update({
                "status": "pending",
                "time_to_send": "07:00"
            }).eq("id", order_id).execute()
            
            # Get the updated order with items
            updated_order = cart_service.sb.table("orders").select("*, order_items(*)").eq("id", order_id).single().execute()
            
            log.info(f"Successfully updated order {order_id} to 'pending' status")
            
            # Trigger background task for content generation
            background_tasks.add_task(
                trigger_chapter_generation,
                order=updated_order.data,
                chapter_service=chapter_service,
                supabase=supabase
            )
            
            log.info(f"Enqueued chapter generation task for order_id: {order_id}")
            
            return {
                "status": "success",
                "message": "Subscription activated and order processed successfully. Content generation has started.",
                "order": updated_order.data,
                "subscription": subscription_data
            }
        else:
            log.warning(f"PayPal subscription {subscription_id} status is {subscription_status}")
            
            # Update PayPal subscription status even if not active yet
            cart_service.update_paypal_subscription_status(
                user_id=user_id,
                subscription_id=subscription_id,
                status=subscription_status.lower()
            )
            
            return {
                "status": "pending",
                "message": f"Subscription status: {subscription_status}",
                "subscription": subscription_data
            }
            
    except HTTPException as e:
        log.error(f"HTTP error verifying PayPal subscription for user {user_id}: {e.detail}")
        raise e
    except Exception as e:
        log.error(f"Unexpected error verifying PayPal subscription for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify PayPal subscription"
        )

@router.post("/capture-order/{order_id}", summary="Capture payment for a one-time order")
async def capture_paypal_order(
    order_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
    paypal_service: PayPalService = Depends(get_paypal_service),
    cart_service: CartService = Depends(get_cart_service),
    chapter_service: ChapterService = Depends(get_chapter_service),
    supabase: Client = Depends(get_supabase)
):
    user_id = user.get("id")
    log.info(f"User {user_id} capturing PayPal order {order_id}")
    try:
        order_from_db = cart_service.get_order_by_paypal_order_id(order_id)
        log.debug(f"Fetched order from DB for PayPal order {order_id}: {order_from_db}")
        if not order_from_db or order_from_db["user_id"] != user_id:
            raise HTTPException(status_code=404, detail="Order not found or access denied.")

        capture_data = await paypal_service.capture_order(order_id)
        log.info(f"PayPal order {order_id} capture response: {capture_data}")
        if capture_data.get("status") == "COMPLETED":
            db_order_id = order_from_db["order_id"]
            log.debug(f"PayPal order {order_id} captured successfully, updating local order {db_order_id}")
            cart_service.sb.table("orders").update({"status": "pending"}).eq("id", db_order_id).execute()
            cart_service.sb.table("direct_orders").update({
                "gateway_order_status": "completed"
            }).eq("order_id", db_order_id).execute() 
            updated_order = cart_service.sb.table("orders").select("*, order_items(*), direct_orders(*)").eq("id", db_order_id).single().execute()
            background_tasks.add_task(trigger_chapter_generation, order=updated_order.data, chapter_service=chapter_service, supabase=supabase)
            return {"status": "success", "message": "Payment captured.", "order": updated_order.data}
        else:
            status = capture_data.get("status", "unknown")
            raise HTTPException(status_code=400, detail=f"Payment capture failed with status: {status}")
    except Exception as e:
        log.error(f"Unexpected error capturing order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to capture PayPal payment")

@router.post("/cancel-subscription/{subscription_id}", summary="Cancel a PayPal subscription")
async def cancel_paypal_subscription(
    subscription_id: str,
    user: dict = Depends(current_user),
    paypal_service: PayPalService = Depends(get_paypal_service)
):
    """Cancels an active PayPal subscription via the API."""
    user_id = user.get("id")
    log.info(f"User {user_id} attempting to cancel PayPal subscription {subscription_id}")
    try:
        # NOTE: You should add logic here to verify from your DB that this subscription belongs to this user.
        success = await paypal_service.cancel_subscription(subscription_id)
        if success:
            log.info(f"Successfully cancelled subscription {subscription_id}")
            # NOTE: You should also update the subscription status in your own database here.
            return {"status": "success", "message": "Subscription has been cancelled."}
        else:
            raise HTTPException(status_code=400, detail="Cancellation request failed.")
    except Exception as e:
        log.error(f"Error cancelling subscription {subscription_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to cancel subscription: {str(e)}")
