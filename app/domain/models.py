# app/domain/models.py
from pydantic import BaseModel, Field, EmailStr
from typing import Any, Dict, Optional, List

class UserProfile(BaseModel):
    full_name: str
    email: Optional[EmailStr] = None
    avatar_url: Optional[str] = None
    credits: Optional[int] = 0

class WishCreate(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300)

class RefineRequest(BaseModel):
    """A simplified model for refining a roadmap, only taking instructions."""
    instructions: str = Field(..., min_length=3, description="The user's instructions to modify the learning roadmap.")

class WishResult(BaseModel):
    wish: Dict[str, Any]
    book: Optional[Dict[str, Any]] = None
    artifact: Optional[str] = None
    roadmap: Dict[str, Any] | Any

class GenerateOneRequest(BaseModel):
    book_id: str
    idx: int
    module_index: int
    topic_index: int
    wish_id: Optional[str] = None

class GenerateAllRequest(BaseModel):
    book_id: str
    wish_id: Optional[str] = None

# --- Cart Models ---

class CartItemAdd(BaseModel):
    """Defines the payload for adding an item to the cart."""
    book_id: str = Field(..., description="The UUID of the book to add.")
    subscription: bool = Field(True, description="Whether the item is a subscription (True) or one-time purchase (False).")

class CartItemUpdate(BaseModel):
    """Defines the payload for updating an item, specifically its subscription type."""
    subscription: bool = Field(..., description="The new subscription status for the book in the cart.")

class CheckoutRequest(BaseModel):
    """Defines the payload for the checkout process, requiring a delivery time."""
    time_to_send: str = Field(
        ..., 
        pattern=r'^([01]\d|2[0-3]):([0-5]\d):([0-5]\d)$',
        description="The preferred time to send content, in HH:MM:SS format.",
        example="07:00:00"
    )
# --- Response Models (Optional but Recommended) ---
class BookInfo(BaseModel):
    generated_title: str

class OrderItem(BaseModel):
    order_id: str
    book_id: str
    subscription: bool
    unit_price: float
    quantity: int
    created_at: str
    updated_at: str
    book: BookInfo

class CartResponse(BaseModel):
    id: str
    user_id: str
    sub_total: float
    discount: float
    total: float
    status: str
    coupon_id: Optional[str]
    currency: str
    created_at: str
    updated_at: str
    time_to_send: str
    items: List[OrderItem] = []

class UserCredentials(BaseModel):
    """Base model for user email and password, used for sign-up and sign-in."""
    email: EmailStr = Field(..., example="user@example.com")
    password: str = Field(..., min_length=8, example="a-very-strong-password")
    name: Optional[str] = Field(None, min_length=3, max_length=100, example="John Doe")

class Token(BaseModel):
    """Represents the core session data returned upon successful authentication."""
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: Optional[EmailStr] = None

class AuthResponse(BaseModel):
    """
    Standard response model for authentication endpoints, wrapping the session token.
    This structure mirrors the object returned by Supabase for consistency.
    """
    session: Token

class OrderItemDetail(BaseModel):
  book_id: str;
  subscription: bool;
  unit_price: float;
  book: BookInfo;

class OrderResponse(BaseModel):
  id: str;
  status: str;
  created_at: str;
  total: float;
  time_to_send: str;
  sub_total: float;
  discount: float;
  items: List[OrderItemDetail];

# --- Credit Purchase Models ---

class CreditPurchaseRequest(BaseModel):
    """Defines the payload for purchasing credits."""
    package_id: str = Field(..., description="The ID of the credit package to purchase")
    credits: int = Field(..., description="Number of credits to purchase")
    price: float = Field(..., description="Price of the credit package")

class CreditPurchaseResponse(BaseModel):
    """Response model for credit purchase operations."""
    status: str
    message: str
    payment_type: str
    approval_url: str
    order_id: Optional[str] = None
    subscription_id: Optional[str] = None

# --- Email Models ---

class EmailRequest(BaseModel):
    """Request model for sending emails."""
    recipient_email: EmailStr = Field(..., description="Email address of the recipient")
    subject: str = Field(..., min_length=1, max_length=200, description="Email subject")
    html_content: str = Field(..., description="HTML content of the email")
    attachment_path: Optional[str] = Field(None, description="Path to file to attach (optional)")

class EmailResponse(BaseModel):
    """Response model for email operations."""
    status: str
    message: str
    recipient: str
    subject: str

# --- PDF Models ---

class PDFGenerationRequest(BaseModel):
    """Request model for PDF generation."""
    book_id: str = Field(..., description="The ID of the book to generate PDF for")
    book_title: str = Field("My Book", description="The title of the book")
    output_path: Optional[str] = Field(None, description="Optional custom path for local output file")

class PDFGenerationResponse(BaseModel):
    """Response model for PDF generation operations."""
    status: str
    message: str
    book_id: str
    book_title: str
    file_size_bytes: int
    storage_url: Optional[str] = None
    local_path: Optional[str] = None

class PDFInfoResponse(BaseModel):
    """Response model for PDF information."""
    book_id: str
    chapter_count: int
    chapters: List[dict]

class ChapterInfo(BaseModel):
    """Model for chapter information."""
    id: str
    index: int
    book_id: str