# app/services/cart_service.py
from __future__ import annotations

from fastapi import HTTPException, status
from supabase import Client
from app.core.pricing import PRICE_SUBSCRIPTION, PRICE_ONE_TIME

from app.core.logging import get_logger

# Instantiate the logger for this specific module
log = get_logger(__name__)

class CartService:
    """
    Manages shopping cart operations, including adding items, updating them,
    and handling the checkout process.
    """
    def __init__(self, supabase: Client):
        """Initializes the service with a Supabase client."""
        self.sb = supabase

    # ----------------------------------------------------------------------------
    # Private Helpers
    # ----------------------------------------------------------------------------

    def _get_or_create_draft_order(self, user_id: str) -> dict:
        """
        Retrieves the user's current 'draft' order. If one doesn't exist, it creates a new one.
        """
        log.info(f"Checking for existing draft order for user_id: {user_id}")
        
        try:
            existing_order = self.sb.table("orders").select("*").eq("user_id", user_id).eq("status", "draft").maybe_single().execute()
        except Exception as e:
            log.error(f"[DB Error] Error fetching draft order for user_id: {user_id}. Error: {e}", exc_info=True)
            existing_order = None

        if existing_order and existing_order.data:
            log.info(f"Found existing draft order_id: {existing_order.data['id']}")
            return existing_order.data

        log.info(f"No draft order found. Creating a new one for user_id: {user_id}")

        try:
            new_order = { "user_id": user_id, "status": "draft" }
            created_order = self.sb.table("orders").insert(new_order).execute()
            log.info(f"Successfully created new draft order_id: {created_order.data[0]['id']}")
            return created_order.data[0]
        except Exception as e:
            log.error(f"[DB Error] Error creating draft order for user_id: {user_id}. Error: {e}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create draft order.")

    def _recalculate_order_totals(self, order_id: str) -> dict:
        """
        Recalculates the sub_total, discount, and total for an order based on its items and coupon.
        """
        log.info(f"Recalculating totals for order_id: {order_id}")

        try:
            items_res = self.sb.table("order_items").select("unit_price, quantity").eq("order_id", order_id).execute()
        except Exception as e:
            log.error(f"[DB Error] Error fetching order items for order_id: {order_id}. Error: {e}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch order items.")
        
        order_items = items_res.data or []
        sub_total = sum(float(item['unit_price']) * item['quantity'] for item in order_items)

        order_res = self.sb.table("orders").select("coupon_id").eq("id", order_id).single().execute()
        order_data = order_res.data if order_res else {}
        
        discount = 0.0
        if order_data.get("coupon_id"):
            log.info(f"Coupon found for order_id: {order_id}. Applying discount.")
            try:
                coupon_res = self.sb.table("coupons").select("coupon_amount").eq("id", order_data["coupon_id"]).single().execute()
            except Exception as e:
                log.error(f"[DB Error] Error fetching coupon for order_id: {order_id}. Error: {e}", exc_info=True)
                coupon_res = None
            if coupon_res and coupon_res.data:
                discount = float(coupon_res.data["coupon_amount"])

        total = max(0, sub_total - discount)
        log.info(f"Recalculation complete for order_id: {order_id}. Subtotal: {sub_total}, Total: {total}")

        try:
            updated_order = self.sb.table("orders").update({
                "sub_total": sub_total, "discount": discount, "total": total,
            }).eq("id", order_id).execute()
            return updated_order.data[0]
        except Exception as e:
            log.error(f"[DB Error] Error updating order totals for order_id: {order_id}. Error: {e}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update order totals.")

    # ----------------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------------

    def add_item_to_cart(self, *, user_id: str, book_id: str, subscription: bool = True) -> dict:
        """Adds a book to the user's draft order (cart)."""
        book = self.sb.table("books").select("id").eq("id", book_id).maybe_single().execute()
        log.info(f"Output of book fetch: {book}")
        if not book.data:
            log.warning(f"Attempt to add non-existent book_id: {book_id} to cart by user_id: {user_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Book with id {book_id} not found.")

        order = self._get_or_create_draft_order(user_id)
        order_id = order["id"]
        
        existing_item = self.sb.table("order_items").select("book_id").eq("order_id", order_id).eq("book_id", book_id).maybe_single().execute()
        if existing_item:
            log.info(f"Book {book_id} already in cart for order {order_id}. Returning current cart state.")
            return self.get_cart(user_id=user_id)

        unit_price = PRICE_SUBSCRIPTION if subscription else PRICE_ONE_TIME
        new_item = {
            "order_id": order_id, "book_id": book_id, "subscription": subscription,
            "unit_price": unit_price, "quantity": 1,
        }

        self.sb.table("order_items").insert(new_item).execute()
        log.info(f"Successfully added book {book_id} to order {order_id}.")

        self._recalculate_order_totals(order_id)
        return self.get_cart(user_id=user_id)

    def remove_item_from_cart(self, *, user_id: str, book_id: str) -> dict:
        """Removes a specific book from the user's cart."""
        order = self._get_or_create_draft_order(user_id)
        
        log.info(f"Removing book_id: {book_id} from order_id: {order['id']}")

        try:
            self.sb.table("order_items").delete().match({
                "order_id": order["id"], "book_id": book_id,
            }).execute()
        except Exception as e:
            log.error(f"[DB Error] Error removing book_id: {book_id} from order_id: {order['id']}. Error: {e}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to remove item from cart.")

        self._recalculate_order_totals(order["id"])
        return self.get_cart(user_id=user_id)

    def update_cart_item(self, *, user_id: str, book_id: str, subscription: bool) -> dict:
        """Updates an item in the cart, specifically its subscription status and price."""
        order = self._get_or_create_draft_order(user_id)
        new_price = PRICE_SUBSCRIPTION if subscription else PRICE_ONE_TIME

        log.info(f"Updating book_id: {book_id} in order_id: {order['id']} to subscription={subscription}")
        self.sb.table("order_items").update({
            "subscription": subscription, "unit_price": new_price,
        }).match({
            "order_id": order["id"], "book_id": book_id,
        }).execute()
        
        self._recalculate_order_totals(order["id"])
        return self.get_cart(user_id=user_id)

    def get_cart(self, user_id: str) -> dict:
        """
        Fetches the user's cart (draft order) and joins with the books table to include titles.
        """
        log.info(f"Fetching cart for user_id: {user_id}")
        
        draft_order = self._get_or_create_draft_order(user_id)
        order_id = draft_order['id']

        cart_res = self.sb.table("orders").select(
            "*, items:order_items(*, book:books(generated_title))"
        ).eq("id", order_id).single().execute()

        if not cart_res.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cart not found.")

        # Calculate total price
        total = sum(item.get('book', {}).get('price', PRICE_SUBSCRIPTION if item.get('subscription') else PRICE_ONE_TIME) for item in cart_res.data.get('items', []))
        cart_res.data['total'] = total
        log.info(f"Successfully fetched cart data: {cart_res.data}")
        return cart_res.data
        
    def checkout(self, *, user_id: str, time_to_send: str) -> dict:
        """Finalizes the purchase after a successful payment."""
        order = self._get_or_create_draft_order(user_id)
        order_id = order["id"]

        items_count_res = self.sb.table("order_items").select("book_id", count="exact").eq("order_id", order_id).execute()
        if items_count_res.count == 0:
            log.warning(f"User {user_id} attempted to checkout an empty cart for order_id: {order_id}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot checkout with an empty cart.")

        log.info(f"Finalizing checkout for user_id: {user_id}, order_id: {order_id}")
        final_order_state = self._recalculate_order_totals(order_id)

        # Update order status
        self.sb.table("orders").update({
            "status": "pending", "time_to_send": time_to_send,
        }).eq("id", final_order_state["id"]).execute()
        
        # Update book status to 'pending' for all books in the order
        order_items = final_order_state.get("order_items", [])
        for item in order_items:
            book_id = item.get("book_id")
            if book_id:
                try:
                    log.info(f"Updating book_id: {book_id} status to 'pending' during checkout")
                    self.sb.table("books").update({"status": "pending"}).eq("id", book_id).execute()
                except Exception as e:
                    log.error(f"Failed to update book status for book_id: {book_id}. Error: {e}", exc_info=True)
                    # Continue with checkout even if book status update fails

        updated_order_response = self.sb.table("orders").select("*, order_items(*), direct_orders(*)").eq("id", final_order_state["id"]).single().execute()

        log.info(f"Order {order_id} status successfully updated to 'pending' for user_id: {user_id}")
        return updated_order_response.data

    def store_paypal_subscription_info(self, *, user_id: str, subscription_id: str, plan_id: str) -> None:
        """Store PayPal subscription information in the user's draft order."""
        order = self._get_or_create_draft_order(user_id)
        order_id = order["id"]
        
        log.info(f"Storing PayPal subscription info for order_id: {order_id}")
        
        # Store PayPal subscription info in the order metadata
        self.sb.table("orders").update({
            "paypal_subscription_id": subscription_id,
            "paypal_plan_id": plan_id,
            "payment_method": "paypal"
        }).eq("id", order_id).execute() # -- Flag
        
        log.info(f"Successfully stored PayPal subscription info for order_id: {order_id}")

    def update_paypal_subscription_status(self, *, user_id: str, subscription_id: str, status: str) -> None:
        """Update PayPal subscription status in the database."""
        log.info(f"Updating PayPal subscription {subscription_id} status to {status}")
        
        # Update the order with the subscription status
        self.sb.table("orders").update({
            "paypal_subscription_status": status
        }).eq("paypal_subscription_id", subscription_id).execute()
        
        log.info(f"Successfully updated PayPal subscription {subscription_id} status to {status}")

    def get_order_by_paypal_subscription_id(self, subscription_id: str) -> dict | None:
        """Get order by PayPal subscription ID."""
        log.info(f"Looking up order by PayPal subscription ID: {subscription_id}")
        
        try:
            order_res = self.sb.table("orders").select("*").eq("paypal_subscription_id", subscription_id).single().execute()
            if order_res.data:
                log.info(f"Found order {order_res.data['id']} for PayPal subscription {subscription_id}")
                return order_res.data
            else:
                log.warning(f"No order found for PayPal subscription {subscription_id}")
                return None
        except Exception as e:
            log.error(f"Error looking up order by PayPal subscription ID {subscription_id}: {e}")
            return None

    def store_paypal_order_info(self, *, user_id: str, order_id: str) -> None:
        """Store PayPal one-time order information in the user's draft order."""
        try:
            order = self._get_or_create_draft_order(user_id)
            db_order_id = order["id"]
            log.info(f"Storing PayPal order info for db_order_id: {db_order_id}")
            self.sb.table("direct_orders").insert({
                "order_id": db_order_id,
                "gateway_order_id": order_id,
                "payment_method": "paypal",
                "gateway_order_status": "active"
            }).execute()
            log.info(f"Successfully stored PayPal order info for db_order_id: {db_order_id}")
        except Exception as e:
            log.error(f"Error storing PayPal order info for user_id: {user_id}, order_id: {order_id}. Error: {e}", exc_info=True)

    def get_order_by_paypal_order_id(self, order_id: str) -> dict | None:
        """Get order by PayPal one-time order ID."""
        log.info(f"Looking up order by PayPal order ID: {order_id}")
        try:
            res = self.sb.table("direct_orders").select(
                "*, order:orders(*)"
            ).eq("gateway_order_id", order_id).single().execute()
            if res.data and res.data.get("order"):
                order_details = res.data.pop("order")
                order_details.update(res.data)
                return order_details
            else:
                log.warning(f"No order found for gateway ID {order_id}")
                return None
        except Exception as e:
            log.error(f"Error looking up order by PayPal order ID {order_id}: {e}")
            return None

    def check_user_credits(self, user_id: str) -> dict:
        """Check user's current credit balance from profiles table."""
        log.info(f"[CREDIT_CHECK] Starting credit check for user_id: {user_id}")
        try:
            profile_res = self.sb.table("profiles").select("credits").eq("user_id", user_id).single().execute()
            log.debug(f"[CREDIT_CHECK] Database response for user_id {user_id}: {profile_res}")
            
            if profile_res.data:
                credits = profile_res.data.get("credits", 0)
                log.info(f"[CREDIT_CHECK] User {user_id} has {credits} credits available")
                return {"credits": credits, "has_profile": True}
            else:
                log.warning(f"[CREDIT_CHECK] No profile found for user_id: {user_id}")
                return {"credits": 0, "has_profile": False}
        except Exception as e:
            log.error(f"[CREDIT_CHECK] Error checking credits for user_id: {user_id}. Error: {e}", exc_info=True)
            return {"credits": 0, "has_profile": False}

    def deduct_credits(self, user_id: str, amount: float) -> bool:
        """Deduct credits from user's profile. Returns True if successful, False otherwise."""
        log.info(f"[CREDIT_DEDUCT] Starting credit deduction for user_id: {user_id}, amount: {amount}")
        try:
            # First check current credits
            log.debug(f"[CREDIT_DEDUCT] Checking current credits before deduction for user_id: {user_id}")
            current_credits = self.check_user_credits(user_id)
            
            if not current_credits["has_profile"]:
                log.error(f"[CREDIT_DEDUCT] No profile found for user_id: {user_id}")
                return False
            
            if current_credits["credits"] < amount:
                log.warning(f"[CREDIT_DEDUCT] Insufficient credits for user_id: {user_id}. Has {current_credits['credits']}, needs {amount}")
                return False
            
            # Deduct credits
            new_credits = int(current_credits["credits"] - amount)
            log.info(f"[CREDIT_DEDUCT] Attempting to update credits for user_id: {user_id} from {current_credits['credits']} to {new_credits}")
            
            update_res = self.sb.table("profiles").update({"credits": new_credits}).eq("user_id", user_id).execute()
            log.debug(f"[CREDIT_DEDUCT] Database update response for user_id {user_id}: {update_res}")
            
            if update_res.data:
                log.info(f"[CREDIT_DEDUCT] Successfully deducted {amount} credits from user_id: {user_id}. New balance: {new_credits}")
                return True
            else:
                log.error(f"[CREDIT_DEDUCT] Failed to update credits for user_id: {user_id}. No data returned from update")
                return False
                
        except Exception as e:
            log.error(f"[CREDIT_DEDUCT] Error deducting credits for user_id: {user_id}. Error: {e}", exc_info=True)
            return False

    def checkout_with_credits(self, *, user_id: str, time_to_send: str) -> dict:
        """Checkout using credits if sufficient, otherwise raise exception."""
        log.info(f"[CREDIT_CHECKOUT] Starting credit-based checkout for user_id: {user_id}, time_to_send: {time_to_send}")
        
        order = self._get_or_create_draft_order(user_id)
        order_id = order["id"]
        log.debug(f"[CREDIT_CHECKOUT] Using order_id: {order_id} for user_id: {user_id}")

        items_count_res = self.sb.table("order_items").select("book_id", count="exact").eq("order_id", order_id).execute()
        if items_count_res.count == 0:
            log.warning(f"[CREDIT_CHECKOUT] User {user_id} attempted to checkout an empty cart for order_id: {order_id}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot checkout with an empty cart.")

        # Recalculate totals to get the current cart total
        log.debug(f"[CREDIT_CHECKOUT] Recalculating order totals for order_id: {order_id}")
        final_order_state = self._recalculate_order_totals(order_id)
        cart_total = final_order_state["total"]
        log.info(f"[CREDIT_CHECKOUT] Cart total calculated: ${cart_total} for order_id: {order_id}")

        # Check if user has sufficient credits
        log.debug(f"[CREDIT_CHECKOUT] Checking user credits for user_id: {user_id}")
        user_credits = self.check_user_credits(user_id)
        if not user_credits["has_profile"]:
            log.error(f"[CREDIT_CHECKOUT] No profile found for user_id: {user_id}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User profile not found.")
        
        if user_credits["credits"] < cart_total:
            log.warning(f"[CREDIT_CHECKOUT] Insufficient credits for user_id: {user_id}. Has {user_credits['credits']}, needs {cart_total}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail=f"Insufficient credits. You have {user_credits['credits']} credits but need {cart_total}."
            )

        # Deduct credits
        log.info(f"[CREDIT_CHECKOUT] Attempting to deduct {cart_total} credits from user_id: {user_id}")
        if not self.deduct_credits(user_id, cart_total):
            log.error(f"[CREDIT_CHECKOUT] Failed to deduct credits for user_id: {user_id}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to deduct credits.")

        log.info(f"[CREDIT_CHECKOUT] Processing credit-based checkout for user_id: {user_id}, order_id: {order_id}")
        
        # Update order status and payment method
        log.debug(f"[CREDIT_CHECKOUT] Updating order status to 'pending' and payment method to 'credits' for order_id: {order_id}")
        self.sb.table("orders").update({
            "status": "pending", 
            "time_to_send": time_to_send,
        }).eq("id", final_order_state["id"]).execute()
        self.sb.table("direct_orders").insert({
            "payment_method": "credits",
            "order_id": final_order_state["id"],
            "gateway_order_status": "completed",
            "gateway_order_id": f"CREDITS-{final_order_state['id']}"
        }).execute()

        # Update book status to 'pending' for all books in the order
        order_items = final_order_state.get("order_items", [])
        for item in order_items:
            book_id = item.get("book_id")
            if book_id:
                try:
                    log.info(f"[CREDIT_CHECKOUT] Updating book_id: {book_id} status to 'pending' during checkout")
                    self.sb.table("books").update({"status": "pending"}).eq("id", book_id).execute()
                except Exception as e:
                    log.error(f"[CREDIT_CHECKOUT] Failed to update book status for book_id: {book_id}. Error: {e}", exc_info=True)
                    # Continue with checkout even if book status update fails

        updated_order_response = self.sb.table("orders").select("*, order_items(*), direct_orders(*)").eq("id", final_order_state["id"]).single().execute()
        log.debug(f"[CREDIT_CHECKOUT] Final order response for order_id {order_id}: {updated_order_response}")
        
        log.info(f"[CREDIT_CHECKOUT] Credit-based checkout completed successfully for order {order_id} for user_id: {user_id}")
        return updated_order_response.data

    def store_cashfree_order_info(self, *, user_id: str, order_id: str, payment_session_id: str) -> None:
        """Store Cashfree order information in the user's draft order."""
        order = self._get_or_create_draft_order(user_id)
        db_order_id = order["id"]
        log.info(f"Storing Cashfree order info for db_order_id: {db_order_id}")
        
        # Update direct_orders table
        self.sb.table("direct_orders").insert({
            "order_id": db_order_id,
            "gateway_order_id": order_id,
            "payment_method": "cashfree",
            "gateway_order_status": "pending"
        }).execute()

        log.info(f"Successfully stored Cashfree order info for db_order_id: {db_order_id}")

    def update_cashfree_order_status(self, *, user_id: str, order_id: str, status: str) -> None:
        """Update Cashfree order status in the database."""
        log.info(f"Updating Cashfree order {order_id} status to {status}")
        
        # Update direct_orders table
        try:
            self.sb.table("direct_orders").update({
                "gateway_order_status": status
            }).eq("gateway_order_id", order_id).execute()
        except Exception as e:
            log.error(f"Error updating Cashfree order status for order_id: {order_id}. Error: {e}", exc_info=True)
            return
        
        log.info(f"Successfully updated Cashfree order {order_id} status to {status}")

    def get_order_by_cashfree_order_id(self, order_id: str) -> dict | None:
        """Get order by Cashfree order ID."""
        log.info(f"Looking up order by Cashfree order ID: {order_id}")
      
        try:
            res = self.sb.table("direct_orders").select(
                "*, order:orders(*)"
            ).eq("gateway_order_id", order_id).single().execute()
            if res.data and res.data.get("order"):
                order_details = res.data.pop("order")
                order_details.update(res.data)
                return order_details
            else:
                log.warning(f"No order found for gateway ID {order_id}")
                return None
        except Exception as e:
            log.error(f"Error looking up order by Cashfree order ID {order_id}: {e}")
            return None