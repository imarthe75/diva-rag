# backend/user_service.py (New File) or within file_processor_service.py
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from backend.models import User # Import your User model
from backend.database import SessionLocal # Assuming you have a way to get a DB session

def register_new_user(username, password, email=None):
    db = SessionLocal()
    try:
        # Hash the password
        hashed_password = generate_password_hash(password)

        new_user = User(username=username, password_hash=hashed_password, email=email)
        db.add(new_user)
        db.commit()
        db.refresh(new_user) # Get the generated ID etc.
        return new_user
    except IntegrityError:
        db.rollback()
        raise ValueError("Username or email already exists.")
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()

def verify_user_login(username, password):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            return user
        return None # Invalid credentials
    finally:
        db.close()
