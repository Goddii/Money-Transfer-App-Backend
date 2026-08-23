"""Validation and parsing for M-Pesa (Daraja) requests."""

from app.utils.errors import ApiError, ErrorCode
from app.utils.validators import (
    normalize_kenyan_phone,
    reject_unexpected_fields,
    require_json_object,
    validate_money_amount,
)

ALLOWED_STK_PUSH_FIELDS = {"amount", "phone"}


def validate_stk_push(data):
    """Validate a user-initiated M-Pesa deposit request."""
    require_json_object(data)
    reject_unexpected_fields(data, ALLOWED_STK_PUSH_FIELDS)

    if "amount" not in data:
        raise ApiError("Amount is required.", 400, ErrorCode.INVALID_AMOUNT)

    amount = validate_money_amount(data.get("amount"))

    if amount != amount.to_integral_value():
        # Daraja only accepts whole-number amounts for STK Push.
        raise ApiError(
            "Amount must be a whole number.", 400, ErrorCode.INVALID_AMOUNT
        )

    phone = normalize_kenyan_phone(data.get("phone"))

    return {"amount": amount, "phone": phone}


def parse_stk_callback(payload):
    """Extract the fields Vyloc relies on from a Daraja STK callback.

    Only values provided by the verified Daraja callback structure are read;
    nothing else in the payload is trusted.
    """
    if not isinstance(payload, dict):
        raise ApiError(
            "Invalid callback payload.", 400, ErrorCode.INVALID_CALLBACK_PAYLOAD
        )

    body = payload.get("Body")
    stk_callback = body.get("stkCallback") if isinstance(body, dict) else None

    if not isinstance(stk_callback, dict):
        raise ApiError(
            "Invalid callback payload.", 400, ErrorCode.INVALID_CALLBACK_PAYLOAD
        )

    checkout_request_id = stk_callback.get("CheckoutRequestID")

    if not checkout_request_id or not isinstance(checkout_request_id, str):
        raise ApiError(
            "Invalid callback payload.", 400, ErrorCode.INVALID_CALLBACK_PAYLOAD
        )

    result_code = stk_callback.get("ResultCode")

    if result_code is None:
        raise ApiError(
            "Invalid callback payload.", 400, ErrorCode.INVALID_CALLBACK_PAYLOAD
        )

    metadata = {}
    callback_metadata = stk_callback.get("CallbackMetadata")

    if isinstance(callback_metadata, dict):
        for item in callback_metadata.get("Item") or []:
            if isinstance(item, dict) and item.get("Name"):
                metadata[item["Name"]] = item.get("Value")

    return {
        "checkout_request_id": checkout_request_id,
        "merchant_request_id": stk_callback.get("MerchantRequestID"),
        "result_code": str(result_code),
        "result_desc": stk_callback.get("ResultDesc"),
        "amount": metadata.get("Amount"),
        "mpesa_receipt_number": metadata.get("MpesaReceiptNumber"),
        "transaction_date": metadata.get("TransactionDate"),
        "phone_number": metadata.get("PhoneNumber"),
    }
