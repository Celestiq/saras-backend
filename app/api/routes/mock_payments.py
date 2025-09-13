# app/api/routes/payments.py
from fastapi import APIRouter, Depends, HTTPException, Header, status
from typing import Optional

from app.db.supabase import get_supabase
from app.services.cart_service import CartService
from app.domain.models import CheckoutRequest
from supabase import Client

router = APIRouter(prefix="/payments", tags=["payments"])

# --- Dependencies (reusing from cart.py) ---

def get_cart_service(supabase: Client = Depends(get_supabase)) -> CartService:
    """Injects the CartService with a Supabase client."""
    return CartService(supabase=supabase)

def get_user_id(x_dev_user_id: Optional[str] = Header(None, alias="X-Dev-User-Id")) -> str:
    """Extracts and validates the user ID from the request header."""
    if not x_dev_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="X-Dev-User-Id header is required for authentication."
        )
    return x_dev_user_id

# --- Mock Payment Endpoint ---

@router.post("/mock-checkout", summary="Simulate a successful payment and checkout")
def mock_payment_and_checkout(
    body: CheckoutRequest,
    user_id: str = Depends(get_user_id),
    service: CartService = Depends(get_cart_service),
):
    """
    **Mock Payment Endpoint for Testing**

    This endpoint simulates a complete, successful payment and checkout flow.
    It performs the following steps:
    1. Fetches the user's current cart (draft order).
    2. Verifies the cart is not empty.
    3. Calls the `cart_service.checkout()` method, which transitions the order
       status from 'draft' to 'pending' and sets the delivery time.
    
    Use this endpoint to test your entire post-purchase workflow without
    implementing a real payment provider.
    """
    try:
        # Step 1: Get the current cart to ensure it exists and has items.
        cart = service.get_cart(user_id=user_id)
        if not cart.get("items"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot process payment for an empty cart."
            )

        # Step 2: "Process" the payment by directly calling the checkout service.
        # This is where you would normally interact with Stripe, get a confirmation,
        # and then call the checkout service upon success.
        finalized_order = service.checkout(
            user_id=user_id, 
            time_to_send=body.time_to_send
        )

        return {
            "status": "mock_payment_successful",
            "message": "Order successfully processed and moved to 'pending'.",
            "order": finalized_order
        }

    except HTTPException as e:
        # Re-raise known HTTP exceptions
        raise e
    except Exception as e:
        # Catch any other unexpected errors
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred during mock checkout: {str(e)}"
        )