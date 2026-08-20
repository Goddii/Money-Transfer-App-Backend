from app.routes.auth_routes import auth_bp
from app.routes.user_routes import user_bp
from app.routes.admin_routes import admin_bp


__all__ = [
    "auth_bp",
    "user_bp",
    "admin_bp",
]