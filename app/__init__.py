from flask import Flask
from app.config import Config
from app.extensions import db, migrate, jwt, cors
from app.utils.errors import register_error_handlers


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config['CORS_ORIGINS'].split(',')}})


    #import models to register them with SQLAlchemy
    from app.models import (
        Beneficiary,
        MpesaTransaction,
        ServicePayment,
        ServiceProvider,
        Transaction,
        User,
        Wallet,
        WalletLedger,
    )

    
    # Register blueprints
    from app.routes import (
        admin_bp,
        auth_bp,
        beneficiary_bp,
        mpesa_bp,
        service_payment_bp,
        transaction_bp,
        user_bp,
        wallet_bp,
    )
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(user_bp, url_prefix='/api/users')
    app.register_blueprint(wallet_bp, url_prefix='/api/wallet')
    app.register_blueprint(beneficiary_bp, url_prefix='/api/beneficiaries')
    app.register_blueprint(transaction_bp, url_prefix='/api/transactions')
    app.register_blueprint(mpesa_bp, url_prefix='/api/mpesa')
    app.register_blueprint(service_payment_bp, url_prefix='/api')
    app.register_blueprint(admin_bp)

    register_error_handlers(app)

    # Start the background reconciliation sweep (no-op under TESTING). This is the
    # fix for deposits stranded in PENDING / RECONCILIATION_PENDING when a
    # callback never arrives or arrives before Daraja finalises the payment.
    from app.services.mpesa_service import MpesaService

    MpesaService.validate_daraja_config(app)
    MpesaService.start_reconciliation_sweeper(app)

    @app.get('/')
    def health_check():
        return {
            "status": "success",
            "message": "Vyloc Api is running!"
        }, 200
    return app
