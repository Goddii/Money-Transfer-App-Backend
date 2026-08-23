from app.routes.auth_routes import auth_bp
from app.routes.user_routes import user_bp
from app.routes.admin_routes import admin_bp
from app.routes.wallet_routes import wallet_bp
from app.routes.beneficiary_routes import beneficiary_bp
from app.routes.transaction_routes import transaction_bp
from app.routes.mpesa_routes import mpesa_bp


__all__ = [
    "auth_bp",
    "user_bp",
    "admin_bp",
    "wallet_bp",
    "beneficiary_bp",
    "transaction_bp",
    "mpesa_bp",
]
