from flask import Flask
from app.config import Config
from app.extensions import db, migrate, jwt, cors


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config['CORS_ORIGINS'].split(',')}})


    #import models to register them with SQLAlchemy
    from app.models.user import User
    from app.models.beneficiary import Beneficiary

    
    # Register blueprints
    from app.routes import auth_bp, user_bp
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(user_bp, url_prefix='/api/users')
    
    @app.get('/')
    def health_check():
        return {
            "status": "success",
            "message": "Vyloc Api is running!"
        }, 200
    return app