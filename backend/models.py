from sqlalchemy import Column, String, LargeBinary, Integer, DateTime, Text, BigInteger
from sqlalchemy.dialects.postgresql import JSONB, UUID # <-- ADD UUID here as well
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func # Para usar funciones de la DB como NOW()
from sqlalchemy.orm import relationship # <-- ADD THIS LINE for relationships
from sqlalchemy import ForeignKey # <-- ADD THIS LINE for ForeignKey

# Importar el tipo VECTOR de pgvector directamente
from pgvector.sqlalchemy import Vector # <--- ¡AÑADE ESTA LÍNEA!

# Define la Base declarativa para tus modelos SQLAlchemy
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    username = Column(String(255), unique=True, nullable=False)
    # Cambiado a 'password_hash' para mantener consistencia con tu código
    password_hash = Column(Text, nullable=False)
    email = Column(String(255), unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    # Relación con EncryptedFile (si no la tienes ya en otro archivo de modelos)
    files = relationship("EncryptedFile", back_populates="user")

    def __repr__(self):
        return f"<User(id='{self.id}', username='{self.username}')>"

class EncryptedFile(Base):
    __tablename__ = 'files' # El nombre de la tabla es 'files'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    # Asume que 'user_id' es una clave foránea que referencia a 'users.id'
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=False) # <-- CAMBIO A FOREING KEY
    ceph_path = Column(Text, nullable=False) # Ruta o clave del objeto en MinIO
    encryption_key_encrypted = Column(LargeBinary, nullable=False) # Clave de encriptación del archivo, encriptada
    original_filename = Column(Text, nullable=False)
    mimetype = Column(Text)
    size_bytes = Column(BigInteger)
    upload_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    file_metadata = Column(JSONB, nullable=True) # Para almacenar metadatos variables

    # Nuevos campos para el estado de procesamiento
    processed_status = Column(String(50), default='pending')
    last_processed_at = Column(DateTime(timezone=True))

    # Relación con User (si no la tienes ya en otro archivo de modelos)
    user = relationship("User", back_populates="files")
    # ### CAMBIOS AQUÍ: Relación con DocumentChunk
    chunks = relationship("DocumentChunk", back_populates="file", cascade="all, delete-orphan")


    def __repr__(self):
        return (f"<EncryptedFile(id='{self.id}', original_filename='{self.original_filename}', "
                                f"user_id='{self.user_id}', status='{self.processed_status}')>")

# ### CAMBIOS AQUÍ: Define el modelo DocumentChunk
class DocumentChunk(Base):
    __tablename__ = 'document_chunks'
    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    file_id = Column(UUID(as_uuid=True), ForeignKey('files.id'), nullable=False) # Clave foránea a la tabla files
    chunk_text = Column(Text, nullable=False)
    # ### IMPORTANTE: Asegúrate de que la dimensión (ej. 768) coincida con tu modelo de embedding de Ollama
    chunk_embedding = Column(Vector(768))
    chunk_order = Column(Integer, nullable=False) # Para mantener el orden original del texto
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relación inversa a EncryptedFile
    file = relationship("EncryptedFile", back_populates="chunks")

    def __repr__(self):
        return f"<DocumentChunk(id='{self.id}', file_id='{self.file_id}', order={self.chunk_order})>"
