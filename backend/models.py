from sqlalchemy import Column, String, LargeBinary, Integer, DateTime, Text, BigInteger, Boolean
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from sqlalchemy import ForeignKey
from pgvector.sqlalchemy import Vector

# IMPORTE BASE DESDE backend.database
from backend.database import Base # <-- ¡CAMBIA ESTA LÍNEA!
# Define la Base declarativa para tus modelos SQLAlchemy
# Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    email = Column(String(255), unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    # Relación con Document
    documents = relationship("Document", back_populates="created_by_user", foreign_keys='Document.created_by')
    modified_documents = relationship("Document", back_populates="last_modified_by_user", foreign_keys='Document.last_modified_by')
    
    # Relación con DocumentVersion (si se necesita saber qué versiones subió un usuario)
    # uploaded_versions = relationship("DocumentVersion", back_populates="uploaded_by_user")


    def __repr__(self):
        return f"<User(id='{self.id}', username='{self.username}')>"

### **Nuevos Modelos para Gestión Documental**

#### `Document` (Representa el documento lógico, con metadatos y versiones)

class Document(Base):
    __tablename__ = 'documents'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    
    # Metadatos generales del documento
    title = Column(String(255), nullable=False) # Título lógico del documento (ej. "Contrato de Arrendamiento - Propiedad X")
    category = Column(String(255), nullable=True) # Ej. "Contratos", "Informes", "Facturas"
    tags = Column(TEXT().as_array(String()), nullable=True, default=[]) # Array de etiquetas (ej. ["legal", "2024", "proyecto-alfa"])
    
    # Información de auditoría
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True) # Quién creó el documento (primera versión)
    last_modified_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    last_modified_by = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True) # Quién modificó por última vez los metadatos o subió una nueva versión

    # Relaciones
    versions = relationship("DocumentVersion", back_populates="document", order_by="DocumentVersion.version_number")
    
    # Relación con usuario creador
    created_by_user = relationship("User", back_populates="documents", foreign_keys=[created_by])
    # Relación con usuario que modificó por última vez
    last_modified_by_user = relationship("User", back_populates="modified_documents", foreign_keys=[last_modified_by])


    def __repr__(self):
        return f"<Document(id='{self.id}', title='{self.title}', category='{self.category}')>"


#### `DocumentVersion` (La antigua `EncryptedFile`, ahora representa una versión específica)

class DocumentVersion(Base):
    # Renombrar la tabla para reflejar su propósito de versionado
    __tablename__ = 'document_versions' # Antigua tabla 'files'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    
    # Clave foránea al documento lógico al que pertenece esta versión
    document_id = Column(UUID(as_uuid=True), ForeignKey('documents.id', ondelete='CASCADE'), nullable=False)
    
    # Detalles específicos de esta versión física del archivo
    ceph_path = Column(Text, nullable=False) # Ruta o clave del objeto en MinIO
    encryption_key_encrypted = Column(LargeBinary, nullable=False) # Clave de encriptación del archivo, encriptada
    original_filename = Column(Text, nullable=False) # Nombre del archivo tal como se subió para esta versión
    mimetype = Column(Text)
    size_bytes = Column(BigInteger)
    upload_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    
    # Metadatos del archivo para esta versión (si aplica, ej. hashes)
    file_metadata = Column(JSONB, nullable=True) 

    # Campos para el versionado
    version_number = Column(Integer, nullable=False) # El número de esta versión (ej. 1, 2, 3...)
    is_latest_version = Column(Boolean, default=True, nullable=False) # Indica si esta es la versión más reciente del documento

    # Posibles valores para processed_status (igual que antes, pero ahora para la versión)
    processed_status = Column(String(50), default='pending')
    last_processed_at = Column(DateTime(timezone=True))
    
    # Si quieres registrar quién subió esta versión específica
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey('users.id'), nullable=True) 

    # Relaciones
    document = relationship("Document", back_populates="versions")
    chunks = relationship("DocumentChunk", back_populates="document_version", cascade="all, delete-orphan")
    
    # uploaded_by_user = relationship("User", back_populates="uploaded_versions") # Si descomentas en User

    # Asegura que haya una única versión "latest" para cada documento,
    # aunque la lógica de la app debe mantener esto.
    # También asegura que el número de versión sea único para un documento.
    __table_args__ = (
        # Asegura que cada documento tenga un único número de versión
        # y una única versión marcada como la más reciente
        # Esto es importante para mantener la consistencia
        # UniqueConstraint('document_id', 'version_number'), # Ya lo definí en la DB SQL
        # UniqueConstraint('document_id', 'is_latest_version', postgresql_where=is_latest_version), # Esto es más complejo en SQLAlchemy
        # Mejor manejar 'is_latest_version' lógicamente en el código
    )

    def __repr__(self):
        return (f"<DocumentVersion(id='{self.id}', document_id='{self.document_id}', "
                f"version_number={self.version_number}, is_latest={self.is_latest_version})>")

class DocumentChunk(Base):
    __tablename__ = 'document_chunks'
    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    
    # Cambiado de file_id a document_version_id
    document_version_id = Column(UUID(as_uuid=True), ForeignKey('document_versions.id', ondelete='CASCADE'), nullable=False) 
    
    chunk_text = Column(Text, nullable=False)
    chunk_embedding = Column(Vector(768)) # Asegúrate de que la dimensión (ej. 768) coincida
    chunk_order = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relación inversa a DocumentVersion
    document_version = relationship("DocumentVersion", back_populates="chunks")

    def __repr__(self):
        return (f"<DocumentChunk(id='{self.id}', document_version_id='{self.document_version_id}', "
                f"order={self.chunk_order})>")
