# app/api/routes/subscriptions.py
from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from app.db.supabase import get_supabase
from app.api.deps import current_user
from app.core.logging import get_logger
from app.domain.models import SubscriptionRequest

# Instantiate the logger for this specific module
log = get_logger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

@router.get("/", summary="Get user subscriptions and order history")
def get_user_subscriptions_and_orders(
    user: dict = Depends(current_user),
    supabase: Client = Depends(get_supabase)
) -> Dict[str, Any]:
    """
    Main endpoint for the "Manage Orders" page. Fetches all of a user's 
    active/paused subscriptions and their separate history of completed one-time purchases.
    """
    user_id = user["id"]
    log.info(f"Fetching subscriptions and order history for user_id: {user_id}")
    
    try:
        # Query for active/paused subscriptions
        subscriptions_res = supabase.table("order_items").select("""
            id,
            subscription_status,
            idx_sent,
            book:books(id, generated_title),
            order:orders(id, created_at)
        """).eq("order.user_id", user_id).eq("subscription", True).in_("subscription_status", ["active", "paused", "completed", "cancelled"]).execute()
        
        # Query for completed one-time purchases
        history_res = supabase.table("orders").select("""
            id,
            status,
            total,
            created_at,
            items:order_items!inner(
                subscription,
                id,
                unit_price,
                book:books(generated_title)
            )
        """).eq("user_id", user_id).eq("status", "completed").execute()
        
        log.info(f"Successfully fetched {len(subscriptions_res.data)} subscriptions and {len(history_res.data)} order history items for user_id: {user_id}")
        
        return {
            "subscriptions": subscriptions_res.data,
            "order_history": history_res.data
        }
        
    except Exception as e:
        log.error(f"Error fetching subscriptions and order history for user_id {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch subscriptions and order history"
        )

@router.post("/pause", summary="Pause a subscription")
def pause_subscription(
    request: SubscriptionRequest,
    user: dict = Depends(current_user),
    supabase: Client = Depends(get_supabase)
) -> Dict[str, Any]:
    """
    Pauses an active subscription. The item_id is the UUID of the record in the order_items table.
    """
    user_id = user["id"]
    item_id = request.item_id
    log.info(f"Pausing subscription for item_id: {item_id}, user_id: {user_id}")
    
    try:
        # First, verify ownership
        item_to_pause = supabase.table("order_items").select("id, subscription_status, order:orders(user_id)").eq("id", item_id).single().execute()
        
        if not item_to_pause.data or item_to_pause.data['order']['user_id'] != user_id:
            log.warning(f"User {user_id} attempted to pause subscription {item_id} they don't own")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to pause this subscription"
            )
        
        if not item_to_pause.data or item_to_pause.data['subscription_status'] != 'active':
            log.warning(f"User {user_id} attempted to pause subscription {item_id} which is not active")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only active subscriptions can be paused"
            )
        
        # Update the subscription status
        response = supabase.table("order_items").update({
            "subscription_status": "paused"
        }).eq("id", item_id).execute()
        
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Subscription not found"
            )
        
        log.info(f"Successfully paused subscription {item_id} for user {user_id}")
        return response.data[0]
        
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error pausing subscription {item_id} for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to pause subscription"
        )

@router.post("/resume", summary="Resume a paused subscription")
def resume_subscription(
    request: SubscriptionRequest,
    user: dict = Depends(current_user),
    supabase: Client = Depends(get_supabase)
) -> Dict[str, Any]:
    """
    Resumes a paused subscription. This calculates the pause duration and updates
    the subscription_progress_offset accordingly.
    """
    user_id = user["id"]
    item_id = request.item_id
    log.info(f"Resuming subscription for item_id: {item_id}, user_id: {user_id}")
    
    try:
        # Fetch the order_item record, verifying ownership and ensuring status is 'paused'
        item_response = supabase.table("order_items").select("""
            id,
            subscription_status,
            idx_sent,
            order:orders(user_id)
        """).eq("id", item_id).single().execute()
        
        if not item_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Subscription not found"
            )
        
        item = item_response.data
        
        # Verify ownership
        if item['order']['user_id'] != user_id:
            log.warning(f"User {user_id} attempted to resume subscription {item_id} they don't own")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to resume this subscription"
            )
        
        # Ensure status is 'paused'
        if item['subscription_status'] != 'paused':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Subscription is not paused"
            )
        
        # Update the subscription
        response = supabase.table("order_items").update({
            "subscription_status": "active",
        }).eq("id", item_id).execute()
        
        log.info(f"Successfully resumed subscription {item_id} for user {user_id} to resume from idx: {item['idx_sent']}")
        return response.data[0]
        
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error resuming subscription {item_id} for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume subscription"
        )

@router.post("/cancel", summary="Cancel a subscription")
def cancel_subscription(
    request: SubscriptionRequest,
    user: dict = Depends(current_user),
    supabase: Client = Depends(get_supabase)
) -> Dict[str, str]:
    """
    Permanently cancels a subscription.
    """
    user_id = user["id"]
    item_id = request.item_id
    log.info(f"Cancelling subscription for item_id: {item_id}, user_id: {user_id}")
    
    try:
        # First, verify ownership
        item_to_cancel = supabase.table("order_items").select("id, order:orders(user_id)").eq("id", item_id).single().execute()
        
        if not item_to_cancel.data or item_to_cancel.data['order']['user_id'] != user_id:
            log.warning(f"User {user_id} attempted to cancel subscription {item_id} they don't own")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to cancel this subscription"
            )
        
        # Update the subscription status
        response = supabase.table("order_items").update({
            "subscription_status": "cancelled"
        }).eq("id", item_id).execute()
        
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Subscription not found"
            )
        
        log.info(f"Successfully cancelled subscription {item_id} for user {user_id}")
        return {
            "status": "success",
            "message": "Subscription cancelled."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error cancelling subscription {item_id} for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel subscription"
        )
