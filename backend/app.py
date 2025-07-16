import os
from flask import Flask, request, jsonify, make_response, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
import logging
from uuid import UUID
import json
from datetime import datetime # ¡Nueva importación!

from flask_jwt_extended import create_access_token, jwt_required, JWTManager, get_jwt_identity

from user_service import register_new_user, verify_user_login
# ¡CAMBIOS AQUÍ! Importa los nuevos modelos
from models import Base, User, Document, DocumentVersion, DocumentChunk
from file_processor_service import FileProcessorService
from tasks import process_uploaded_file, get_ollama_embedding, get_ollama_generation

# Carga las variables de entorno
dotenv_path = os.path.join(os.path.dirname(__file__), '..', 'nuevo1')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    load_dotenv()

# --- Configuración de Logging (sin cambios) ---
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuración de la Base de Datos (sin cambios, excepto en create_tables) ---
POSTGRES_HOST = os.getenv("POSTGRES_HOST")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
engine = None

def init_db_engine():
    global engine
    if engine is None:
        try:
            engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20)
            logging.info("Motor de base de datos PostgreSQL inicializado.")
            return engine
        except Exception as e:
            logging.error(f"Error al inicializar el motor de la base de datos: {e}")
            raise

Session = scoped_session(sessionmaker(autocommit=False, autoflush=False))

def create_tables():
    """
    Crea o actualiza todas las tablas definidas en Base.metadata en la base de datos.
    """
    if Base is None:
        logging.error("No se pudo crear tablas: 'Base' no está definido. Revisa models.py y su importación.")
        return False

    logging.info("Intentando crear/actualizar las tablas de la base de datos...")
    try:
        if engine:
            with engine.connect() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                connection.commit()
                logging.info("Extensión 'vector' asegurada en PostgreSQL.")

            Base.metadata.create_all(engine)
            logging.info("¡Tablas de la base de datos creadas/actualizadas exitosamente!")
            return True
        else:
            logging.error("No se pudo obtener el motor de la base de datos para crear las tablas. Asegúrate de llamar init_app_db_session() al inicio.")
            return False
    except SQLAlchemyError as e:
        logging.error(f"Error de SQLAlchemy al crear las tablas: {e}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Error inesperado al crear las tablas de la base de datos: {e}", exc_info=True)
        return False

# --- Configuración de la Aplicación Flask (sin cambios) ---
app = Flask(__name__)
CORS(app)

app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "super-secret-jwt-key")
jwt = JWTManager(app)

def init_app_db_session():
    global engine
    if engine is None:
        engine = init_db_engine()
    Session.configure(bind=engine)
    logging.info("Sesión de SQLAlchemy configurada con el motor de base de datos.")

with app.app_context():
    init_app_db_session()
    create_tables()


# Configuración del servicio de procesamiento de archivos (sin cambios)
app.config['FILE_PROCESSOR_SERVICE'] = FileProcessorService(
    s3_endpoint_url=os.getenv("CEPH_ENDPOINT_URL"),
    s3_access_key=os.getenv("CEPH_ACCESS_KEY"),
    s3_secret_key=os.getenv("CEPH_SECRET_KEY"),
    s3_bucket_name=os.getenv("CEPH_BUCKET_NAME"),
    master_key=os.getenv("SYSTEM_MASTER_KEY"),
    kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS"),
    kafka_topic_uploaded=os.getenv("KAFKA_TOPIC_FILE_UPLOADED")
)

# Middleware para la sesión de la base de datos (sin cambios)
@app.before_request
def before_request():
    request.db_session = Session()

@app.teardown_request
def teardown_request(exception=None):
    if hasattr(request, 'db_session'):
        Session.remove()

# Resto de las rutas que no se modifican directamente aquí (login, register, test-db, home)
# ...

# --- Rutas de la API ---

# Home
@app.route('/')
def home():
    return "Digital Vault Project API is running!"

@app.route('/vault/test-db', methods=['GET'])
def test_db_connection():
    session = request.db_session
    try:
        session.execute("SELECT 1")
        return jsonify({"message": "Database connection successful!"}), 200
    except Exception as e:
        logging.error(f"Error testing database connection: {e}", exc_info=True)
        return jsonify({"error": "Database connection failed", "details": str(e)}), 500

@app.route('/register', methods=['POST'])
def register_user_endpoint():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    email = data.get('email')

    if not username or not password:
        return jsonify({"message": "Username and password are required"}), 400

    try:
        new_user = register_new_user(username, password, email)
        return jsonify({"message": "User registered successfully", "user_id": str(new_user.id)}), 201
    except ValueError as e:
        return jsonify({"message": str(e)}), 409
    except Exception as e:
        logging.error(f"Error during user registration: {e}", exc_info=True)
        return jsonify({"message": "An unexpected error occurred during registration."}), 500

@app.route('/login', methods=['POST'])
def login_user_endpoint():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"message": "Username and password are required"}), 400

    user = verify_user_login(username, password)
    if user:
        # Crea un token de acceso para el usuario autenticado
        access_token = create_access_token(identity=str(user.id)) # La identidad será el UUID del usuario
        return jsonify(access_token=access_token, user_id=str(user.id)), 200
    else:
        return jsonify({"message": "Invalid username or password"}), 401


# --- Rutas de la API (CONTINUACIÓN) ---

#Lógica de Carga de Archivos (/documents) 🚀
#Vamos a reemplazar upload_file por un endpoint /documents que maneje tanto la creación de nuevos documentos como la adición de nuevas versiones.

@app.route('/documents', methods=['POST'])
@jwt_required()
def upload_document():
    current_user_id_str = get_jwt_identity()
    user_id = UUID(current_user_id_str)
    session = request.db_session

    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    # Obtener metadatos del formulario
    title = request.form.get('title', file.filename.rsplit('.', 1)[0] if '.' in file.filename else file.filename)
    category = request.form.get('category')
    tags_str = request.form.get('tags')
    tags = [tag.strip() for tag in tags_str.split(',')] if tags_str else []

    # Opcional: Obtener document_id para subir una nueva versión
    existing_document_id_str = request.form.get('document_id')
    existing_document_id = None
    if existing_document_id_str:
        try:
            existing_document_id = UUID(existing_document_id_str)
        except ValueError:
            return jsonify({"error": "Invalid document_id format provided"}), 400

    try:
        file_processor = app.config['FILE_PROCESSOR_SERVICE']

        document = None
        new_version_number = 1

        if existing_document_id:
            # Subir una nueva versión de un documento existente
            document = session.query(Document).filter_by(id=existing_document_id, created_by=user_id).first()
            if not document:
                return jsonify({"error": "Document not found or you don't have permission to add a version to it."}), 404

            # Desmarcar la versión anterior como la más reciente
            session.query(DocumentVersion).filter_by(document_id=existing_document_id, is_latest_version=True).update(
                {"is_latest_version": False}
            )
            
            # Obtener el número de la última versión y añadir 1
            last_version = session.query(DocumentVersion).filter_by(document_id=existing_document_id)\
                                  .order_by(DocumentVersion.version_number.desc()).first()
            if last_version:
                new_version_number = last_version.version_number + 1
            else: # Debería haber al menos una versión si el documento existe
                logging.warning(f"Document {existing_document_id} found but no versions exist. Starting from 1.")
                new_version_number = 1 

            # Actualizar metadatos del documento lógico si se proporcionan
            document.title = title
            document.category = category
            document.tags = tags
            document.last_modified_by = user_id
            document.last_modified_at = datetime.now() # SQLAlchemy debería manejar onupdate, pero explícito no está mal.
            session.add(document) # Marcar para actualización
            logging.info(f"Adding new version {new_version_number} for document {document.id}")

        else:
            # Crear un nuevo documento y su primera versión
            document = Document(
                title=title,
                category=category,
                tags=tags,
                created_by=user_id,
                last_modified_by=user_id
            )
            session.add(document)
            session.flush() # Para obtener el document.id antes de usarlo en DocumentVersion
            logging.info(f"Creating new document with ID: {document.id}")

        # Ahora creamos la nueva entrada en DocumentVersion
        # `process_and_store_file` manejará la carga a MinIO y encriptación
        # y devolverá la información necesaria para crear DocumentVersion
        file_info = file_processor.process_and_store_file(file, user_id, session) # user_id is uploaded_by here

        new_document_version = DocumentVersion(
            document_id=document.id,
            version_number=new_version_number,
            is_latest_version=True,
            ceph_path=file_info['ceph_path'],
            encryption_key_encrypted=file_info['encryption_key_encrypted'],
            original_filename=file.filename,
            mimetype=file.mimetype,
            size_bytes=file.content_length,
            processed_status='pending',
            uploaded_by=user_id
            # file_metadata (JSONB) no se está usando directamente aquí, podrías pasarlo si es relevante a la versión.
        )
        session.add(new_document_version)
        session.commit() # ¡Commit aquí para guardar el documento y la versión!

        logging.info(f"Despachando tarea Celery para document_version_id: {new_document_version.id} y ceph_path: {new_document_version.ceph_path}")
        # Pasa el ID de la DocumentVersion, no el del Document
        process_uploaded_file.delay(str(new_document_version.id), new_document_version.ceph_path, new_document_version.original_filename)

        return jsonify({
            "message": "Document uploaded/new version created and processing started",
            "document_id": str(document.id),
            "document_version_id": str(new_document_version.id),
            "version_number": new_document_version.version_number
        }), 200

    except Exception as e:
        session.rollback()
        logging.error(f"Error processing document upload/new version: {e}", exc_info=True)
        return jsonify({"error": "Internal server error during document upload", "details": str(e)}), 500


# Listar Documentos (GET /documents)
# Este endpoint permitirá listar los documentos lógicos (agrupando sus versiones) con opciones de filtrado.
@app.route('/documents', methods=['GET'])
@jwt_required()
def list_documents():
    current_user_id = UUID(get_jwt_identity())
    session = request.db_session

    try:
        query = session.query(Document).filter_by(created_by=current_user_id)

        # Filtros (opcionales)
        category = request.args.get('category')
        if category:
            query = query.filter(Document.category == category)
        
        tag = request.args.get('tag') # Para buscar documentos que contengan una etiqueta específica
        if tag:
            # Usar contains para buscar en el array de tags
            query = query.filter(Document.tags.contains([tag])) 
        
        search_term = request.args.get('search')
        if search_term:
            query = query.filter(Document.title.ilike(f'%{search_term}%'))

        # Ordenar (ej. por fecha de última modificación)
        query = query.order_by(Document.last_modified_at.desc())

        documents = query.all()

        documents_data = []
        for doc in documents:
            latest_version = session.query(DocumentVersion)\
                                    .filter_by(document_id=doc.id, is_latest_version=True)\
                                    .first()
            
            # Puedes decidir si incluir solo la última versión o todas
            documents_data.append({
                "id": str(doc.id),
                "title": doc.title,
                "category": doc.category,
                "tags": doc.tags,
                "created_at": doc.created_at.isoformat(),
                "last_modified_at": doc.last_modified_at.isoformat(),
                "latest_version_info": {
                    "id": str(latest_version.id),
                    "version_number": latest_version.version_number,
                    "original_filename": latest_version.original_filename,
                    "processed_status": latest_version.processed_status,
                    "upload_timestamp": latest_version.upload_timestamp.isoformat()
                } if latest_version else None
            })
        
        return jsonify(documents_data), 200

    except Exception as e:
        logging.error(f"Error listing documents for user {current_user_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error while listing documents", "details": str(e)}), 500

# Actualizar Metadatos del Documento (PUT /documents/<document_id>) 📝
# Este endpoint permite a los usuarios actualizar el título, categoría o etiquetas de un documento lógico, no de una versión específica.    

@app.route('/documents/<uuid:document_id>', methods=['PUT'])
@jwt_required()
def update_document_metadata(document_id):
    current_user_id = UUID(get_jwt_identity())
    session = request.db_session
    data = request.get_json()

    try:
        document = session.query(Document).filter_by(id=document_id, created_by=current_user_id).first()
        if not document:
            return jsonify({"error": "Document not found or you don't have permission to update it."}), 404

        # Actualizar campos si están presentes en la solicitud
        if 'title' in data:
            document.title = data['title']
        if 'category' in data:
            document.category = data['category']
        if 'tags' in data and isinstance(data['tags'], list):
            document.tags = data['tags']
        
        document.last_modified_by = current_user_id
        # `last_modified_at` se actualiza automáticamente con `onupdate=func.now()` en el modelo.

        session.commit()
        return jsonify({"message": "Document metadata updated successfully", "document_id": str(document.id)}), 200

    except Exception as e:
        session.rollback()
        logging.error(f"Error updating metadata for document {document_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error while updating document metadata", "details": str(e)}), 500


#Obtener Versiones de un Documento (GET /documents/<document_id>/versions) 📜
#Permite ver el historial de versiones de un documento.

@app.route('/documents/<uuid:document_id>/versions', methods=['GET'])
@jwt_required()
def list_document_versions(document_id):
    current_user_id = UUID(get_jwt_identity())
    session = request.db_session

    try:
        # Verifica que el documento exista y pertenezca al usuario
        document = session.query(Document).filter_by(id=document_id, created_by=current_user_id).first()
        if not document:
            return jsonify({"error": "Document not found or you don't have permission to view its versions."}), 404

        versions = session.query(DocumentVersion)\
                          .filter_by(document_id=document_id)\
                          .order_by(DocumentVersion.version_number.asc())\
                          .all()

        versions_data = []
        for version in versions:
            versions_data.append({
                "id": str(version.id),
                "version_number": version.version_number,
                "original_filename": version.original_filename,
                "is_latest_version": version.is_latest_version,
                "processed_status": version.processed_status,
                "upload_timestamp": version.upload_timestamp.isoformat(),
                "ceph_path": version.ceph_path # Puedes decidir si quieres exponer esto o no
            })
        
        return jsonify(versions_data), 200

    except Exception as e:
        logging.error(f"Error listing versions for document {document_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error while listing document versions", "details": str(e)}), 500


# Descarga un archivo específico (GET /documents/versions/<version_id>/download) ⬇️
# Este es el endpoint para descargar una versión específica de un documento.

@app.route('/documents/versions/<uuid:version_id>/download', methods=['GET'])
@jwt_required()
def download_document_version(version_id):
    try:
        current_user_id = UUID(get_jwt_identity())
        session = request.db_session

        # Encuentra la versión del documento
        document_version = session.query(DocumentVersion).filter_by(id=version_id).first()

        if not document_version:
            return jsonify({"error": "Document version not found"}), 404

        # Verifica los permisos del usuario (que sea dueño del documento lógico)
        document = session.query(Document).filter_by(id=document_version.document_id, created_by=current_user_id).first()
        if not document:
            logging.warning(f"Unauthorized download attempt for version {version_id} by user {current_user_id}. Document owner mismatch.")
            return jsonify({"error": "Unauthorized access: You do not have permission to download this document version"}), 403

        file_processor = app.config['FILE_PROCESSOR_SERVICE']
        decrypted_data = file_processor.retrieve_and_decrypt_file(document_version) # Pasar el objeto DocumentVersion directamente

        response = make_response(decrypted_data)
        response.headers.set('Content-Type', document_version.mimetype)
        response.headers.set('Content-Disposition', 'attachment', filename=document_version.original_filename)
        return response

    except Exception as e:
        logging.error(f"Error downloading document version {version_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error during file download", "details": str(e)}), 500


# Eliminar un Documento Lógico Completo (DELETE /documents/<document_id>) 🗑️
# Este endpoint eliminará un documento lógico y, debido a ON DELETE CASCADE en la base de datos, todas sus versiones asociadas y sus chunks de RAG se eliminarán automáticamente. Además, eliminará los archivos físicos de MinIO.

@app.route('/documents/<uuid:document_id>', methods=['DELETE'])
@jwt_required()
def delete_document(document_id):
    current_user_id = UUID(get_jwt_identity())
    session = request.db_session

    try:
        document = session.query(Document).filter_by(id=document_id, created_by=current_user_id).first()
        if not document:
            return jsonify({"error": "Document not found or you don't have permission to delete it."}), 404

        file_processor = app.config['FILE_PROCESSOR_SERVICE']
        
        # Obtener todas las versiones para eliminar los archivos de MinIO
        versions_to_delete = session.query(DocumentVersion).filter_by(document_id=document_id).all()
        for version in versions_to_delete:
            try:
                file_processor.delete_file_from_minio(version.ceph_path)
                logging.info(f"Deleted file {version.ceph_path} from MinIO for document version {version.id}")
            except Exception as e:
                logging.error(f"Failed to delete file {version.ceph_path} from Minio for version {version.id}: {e}")
                # Considerar si parar o continuar si falla la eliminación de MinIO

        # Eliminar el documento lógico. Esto, gracias a ON DELETE CASCADE,
        # eliminará automáticamente todas las entradas relacionadas en document_versions y document_chunks.
        session.delete(document)
        session.commit()

        return jsonify({"message": f"Document {document_id} and all its versions deleted successfully."}), 200

    except Exception as e:
        session.rollback()
        logging.error(f"Error deleting document {document_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error while deleting document", "details": str(e)}), 500


# --- Ruta para Consultas RAG ---
@app.route('/ask', methods=['POST'])
@jwt_required() # Protege este endpoint
def ask_question():
    user_question = request.json.get('question')
    if not user_question:
        return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

    current_user_id_str = get_jwt_identity()
    user_id_from_token = UUID(current_user_id_str)

    # 1. Obtener embedding de la pregunta del usuario
    OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL")
    question_embedding = get_ollama_embedding(user_question, model_name=OLLAMA_EMBEDDING_MODEL)
    if question_embedding is None: # Asegúrate de manejar el caso donde el embedding sea None
        return jsonify({"error": "No se pudo generar el embedding de la pregunta."}), 500

    retrieved_chunks = []
    session = request.db_session

    try:
        # Consulta SQL para buscar chunks relevantes
        # Realiza un JOIN para filtrar por los documentos del usuario y la última versión
        # Se usa document_versions.is_latest_version = TRUE para asegurar que se consulta
        # solo la versión más reciente y relevante de cada documento.
        result = session.execute(
            text("""
                SELECT
                    dc.chunk_text
                FROM
                    document_chunks dc
                JOIN
                    document_versions dv ON dc.document_version_id = dv.id
                JOIN
                    documents d ON dv.document_id = d.id
                WHERE
                    d.created_by = :user_id AND dv.is_latest_version = TRUE AND dv.processed_status = 'indexed'
                ORDER BY
                    dc.chunk_embedding <=> CAST(:embedding AS vector)
                LIMIT 5;
            """),
            {"embedding": question_embedding, "user_id": user_id_from_token}
        )
        retrieved_chunks = [row.chunk_text for row in result.fetchall()]

    except Exception as e:
        logging.error(f"Error al buscar en la base de datos para usuario {user_id_from_token}: {e}", exc_info=True)
        return jsonify({"error": "Error al buscar información relevante en los documentos del usuario."}), 500

    if not retrieved_chunks:
        return jsonify({"answer": "No pude encontrar información relevante en los documentos indexados disponibles para ti."})

    # 3. Construir el prompt para el modelo de generación
    context = "\n".join(retrieved_chunks)

    prompt_for_llm = (
        f"Basado en el siguiente contexto, responde a la pregunta. "
        f"Si la respuesta no se encuentra directamente en el contexto, indica que no tienes suficiente información "
        f"y no intentes inventar la respuesta.\n\n"
        f"Contexto:\n{context}\n\n"
        f"Pregunta: {user_question}\n"
        f"Respuesta:"
    )

    logging.info(f"Enviando prompt al LLM: {prompt_for_llm[:200]}...")

    # 4. Obtener la respuesta del modelo de generación
    OLLAMA_GENERATION_MODEL = os.getenv("OLLAMA_GENERATION_MODEL")
    llm_response = get_ollama_generation(prompt_for_llm, model_name=OLLAMA_GENERATION_MODEL)

    return jsonify({"answer": llm_response})


# --- Punto de entrada principal ---
if __name__ == '__main__':
    logging.info("Starting Flask app in development mode (if __name__ == '__main__':)")
    app.run(debug=True, host='0.0.0.0', port=5000)
