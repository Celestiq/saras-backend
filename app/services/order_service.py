from __future__ import annotations
from supabase import Client
from app.core.logging import get_logger

log = get_logger(__name__)

class OrderService:
    """Handles business logic for retrieving user orders."""
    def __init__(self, supabase: Client):
        self.sb = supabase

    def get_orders_for_user(self, user_id: str) -> list[dict]:
        """
        Fetches all orders for a given user, sorted by creation date,
        and joins with related tables to include item and book details.
        """
        log.info(f"Fetching all orders for user_id: {user_id}")
        try:
            # This query fetches all orders for the user and, for each order,
            # it fetches the associated 'order_items' and the 'title' from the related 'books' table.
            orders_res = self.sb.table("orders").select(
                "*, items:order_items(*, book:books(generated_title)), direct_orders(*)"
            ).eq("user_id", user_id).order("created_at", desc=True).execute()
            
            log.info(f"Successfully fetched {len(orders_res.data)} orders for user_id: {user_id}")
            log.debug(f"Orders data: {orders_res.data}")
            return orders_res.data
        except Exception as e:
            log.error(f"Error fetching orders for user_id: {user_id}. Error: {e}", exc_info=True)
            return []

    def get_order_by_id(self, order_id: str, user_id: str) -> dict | None:
        """
        Fetches a specific order by ID for a given user.
        Returns None if the order doesn't exist or doesn't belong to the user.
        """
        log.info(f"Fetching order {order_id} for user_id: {user_id}")
        try:
            order_res = self.sb.table("orders").select(
                "*, items:order_items(*, book:books(generated_title)), direct_orders(*)"
            ).eq("id", order_id).eq("user_id", user_id).maybe_single().execute()
            
            if order_res.data:
                log.info(f"Successfully fetched order {order_id} for user_id: {user_id}")
                log.debug(f"Order data: {order_res.data}")
                return order_res.data
            else:
                log.warning(f"Order {order_id} not found or doesn't belong to user {user_id}")
                return None
        except Exception as e:
            log.error(f"Error fetching order {order_id} for user_id: {user_id}. Error: {e}", exc_info=True)
            return None