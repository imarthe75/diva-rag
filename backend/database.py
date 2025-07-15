# backend/database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv() # Load environment variables from .env

# Get database connection details from environment variables
# Es preferible usar la variable DATABASE_URL si está definida,
# de lo contrario, construirla a partir de las individuales.
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DB_USER = os.getenv("POSTGRES_USER", "dvu")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "secret")
    DB_NAME = os.getenv("POSTGRES_DB", "digital_vault_db")
    DB_HOST = os.getenv("POSTGRES_HOST", "postgres_db")
    # Coincide con el nombre de tu servicio docker-compose
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# Create the SQLAlchemy engine with connection pooling options
# pool_pre_ping=True: Helps with connection stability by testing connections before use.
# pool_size: Number of connections to keep open in the pool.
# max_overflow: Max number of connections to open beyond pool_size when needed.
# pool_recycle: Recycles connections after a specified time to prevent stale connections.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,        
    # Número de conexiones a mantener en el pool (ajustar según carga)
    max_overflow=20,     
    # Número extra de conexiones que se pueden abrir en picos
    pool_recycle=3600    
    # Recicla conexiones cada 1 hora (3600 segundos)
)

# Create a SessionLocal class (a sessionmaker factory)
# Each instance of SessionLocal will be a database session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for your declarative models
# Define Base here, and import it into models.py
Base = declarative_base()

def get_db():
    """Dependency for getting a database session.
    Use with: `with get_db() as db_session:`
    This context manager ensures the session is closed, releasing the connection.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()