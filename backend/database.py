# backend/database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv() # Load environment variables from .env

# Get database connection details from environment variables
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Fallback for local testing or if .env isn't properly loaded for some reason
    DB_USER = os.getenv("POSTGRES_USER", "dvu")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "secret")
    DB_NAME = os.getenv("POSTGRES_DB", "digital_vault_db")
    DB_HOST = os.getenv("POSTGRES_HOST", "postgres-db") # This should match your docker-compose service name
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# Create the SQLAlchemy engine
# pool_pre_ping=True helps with connection stability over long periods
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Create a SessionLocal class (a sessionmaker factory)
# Each instance of SessionLocal will be a database session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for your declarative models (if not already defined in models.py)
# If Base is already defined in models.py and imported there, you might not need this line here
# but if you need Base in other files (like for Base.metadata.create_all), keep it.
Base = declarative_base()

def get_db():
    """Dependency for getting a database session.
    Use in Flask routes like: db_session = get_db()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
