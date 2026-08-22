from app import create_app
from app.extensions import db
from app.models.user import User

app = create_app()
with app.app_context():
    admin_email = 'admin@example.com'
    existing = User.query.filter_by(email=admin_email).first()
    if existing:
        print(f'Admin already exists with id={existing.id}')
    else:
        admin = User(
            first_name='Admin',
            last_name='User',
            email=admin_email,
            role='admin',
            is_active=True,
            status='Active'
        )
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print(f'Admin created with id={admin.id}, email={admin.email}, password=admin123')