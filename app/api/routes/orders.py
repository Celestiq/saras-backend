# app/api/routes/orders.py
from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from typing import List

from app.db.supabase import get_supabase
from app.services.order_service import OrderService
from app.api.deps import current_user
from app.domain.models import OrderResponse # This will be our new response model
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/orders", tags=["orders"])

def get_order_service(supabase: Client = Depends(get_supabase)) -> OrderService:
    return OrderService(supabase)

@router.get("", response_model=List[OrderResponse], summary="Get all orders for the current user")
def get_order_history(
    user: dict = Depends(current_user),
    service: OrderService = Depends(get_order_service)
):
    """
    Retrieves a list of all past and pending orders for the currently
    authenticated user, sorted from newest to oldest.
    """
    log.debug(f"User {user} is fetching their order history.")
    user_id = user.get("id")
    orders = service.get_orders_for_user(user_id)
    log.debug(f"Fetched {len(orders)} orders for user {user}.")
    return orders

@router.get("/{order_id}", response_model=OrderResponse, summary="Get order details by ID")
def get_order_by_id(
    order_id: str,
    user: dict = Depends(current_user),
    service: OrderService = Depends(get_order_service)
):
    """
    Retrieves detailed information about a specific order by its ID.
    Only returns orders that belong to the currently authenticated user.
    """
    log.debug(f"User {user.get('id')} is fetching order {order_id}.")
    user_id = user.get("id")
    order = service.get_order_by_id(order_id, user_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found or you don't have permission to view this order."
        )
    log.debug(f"Successfully fetched order {order_id} for user {user_id}.")
    return order
