import os
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from dotenv import load_dotenv
from sqlalchemy import create_engine, text # ### CAMBIOS AQUÍ: Añade 'text'
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
import logging
from uuid import UUID
import json

# Importar Flask-JWT-Extended
from flask_jwt_extended import create_access_token, jwt_required, JWTManager, get_jwt_identity

from user_service import register_new_user, verify_user_login
from models import Base, EncryptedFile, User
from file_processor_service import FileProcessorService
from tasks import process_uploaded_file # <-- Añade esta línea

# ### CAMBIOS AQUÍ: Importa las funciones de Ollama
# Asumiendo que get_ollama_embedding y get_ollama_generation están en tasks.py
# Si están en otro archivo (ej. ollama_service.py), cambia 'tasks' por el nombre de tu módulo
from tasks import get_ollama_embedding, get_ollama_generation
# Si get_ollama_embedding y get_ollama_generation fueran métodos de una clase en otro archivo, la importación sería diferente.
# Por ejemplo, si estuvieran en un archivo ollama_utils.py:
# from ollama_utils import get_ollama_embedding, get_ollama_generation

# Carga las variables de entorno
dotenv_path = os.path.join(os.path.dirname(__file__), '..', 'nuevo1')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    load_dotenv()

# --- Configuración de Logging ---
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuración de la Base de Datos ---
POSTGRES_HOST = os.getenv("POSTGRES_HOST")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
engine = None

def init_db_engine():
    """Inicializa y retorna el motor de la base de datos."""
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
            # ### CAMBIOS AQUÍ: Habilitar la extensión 'vector' antes de crear las tablas
            with engine.connect() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                connection.commit() # Importante para que el cambio se aplique
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

# --- Configuración de la Aplicación Flask ---
app = Flask(__name__)
CORS(app)

# Configuración de JWT
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "super-secret-jwt-key") # ¡Cámbiala en producción!
jwt = JWTManager(app)

# Función para inicializar el motor y configurar la sesión de SQLAlchemy
def init_app_db_session():
    """Inicializa el motor de DB y configura el bind para la sesión."""
    global engine
    if engine is None:
        engine = init_db_engine()
    Session.configure(bind=engine)
    logging.info("Sesión de SQLAlchemy configurada con el motor de base de datos.")

with app.app_context():
    init_app_db_session()
    create_tables()


# Configuración del servicio de procesamiento de archivos
app.config['FILE_PROCESSOR_SERVICE'] = FileProcessorService(
    s3_endpoint_url=os.getenv("CEPH_ENDPOINT_URL"),
    s3_access_key=os.getenv("CEPH_ACCESS_KEY"),
    s3_secret_key=os.getenv("CEPH_SECRET_KEY"),
    s3_bucket_name=os.getenv("CEPH_BUCKET_NAME"),
    master_key=os.getenv("SYSTEM_MASTER_KEY"),
    kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS"),
    kafka_topic_uploaded=os.getenv("KAFKA_TOPIC_FILE_UPLOADED")
)

# Middleware para la sesión de la base de datos
@app.before_request
def before_request():
    request.db_session = Session()

@app.teardown_request
def teardown_request(exception=None):
    if hasattr(request, 'db_session'):
        Session.remove()

# --- Rutas de la API ---

@app.route('/vault/upload', methods=['POST'])
@jwt_required() # Protege también el endpoint de subida
def upload_file():
    current_user_id_str = get_jwt_identity() # Obtiene el user_id del token
    user_id = UUID(current_user_id_str) # Convierte a UUID si tu DB lo requiere así

    # La lógica de creación de "testuser" debería ser eliminada en un entorno real
    # o movida a un script de inicialización/tests
    session = request.db_session
    user = session.query(User).filter_by(id=user_id).first() # Asegura que el usuario del token existe
    if not user:
        return jsonify({"error": "Authenticated user not found"}), 404

    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    # --- NEW: Get metadata from request form ---
    metadata_str = request.form.get('file_metadata')
    parsed_metadata = None
    if metadata_str:
        try:
            parsed_metadata = json.loads(metadata_str)
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON in metadata field: {metadata_str}")
            return jsonify({"error": "Invalid JSON format for metadata"}), 400
    # --- END NEW ---

    if file:
        try:
            file_processor = app.config['FILE_PROCESSOR_SERVICE']

            # file_info = file_processor.process_and_store_file(file, user_id, request.db_session)
            file_info = file_processor.process_and_store_file(file, user_id, request.db_session, parsed_metadata)

            # --- ¡MODIFICA ESTA SECCIÓN! ---
            if 'id' in file_info and 'ceph_path' in file_info: # Asegúrate que 'id' y 'ceph_path' existan
                logging.info(f"Despachando tarea Celery para file_id: {file_info['id']} y ceph_path: {file_info['ceph_path']}")
                # Pasa ambos argumentos: file_id_str y ceph_path
                process_uploaded_file.delay(str(file_info['id']), file_info['ceph_path']) # <-- ¡CAMBIA ESTA LÍNEA!
            else:
                logging.warning("No se encontró 'id' o 'ceph_path' en file_info para despachar tarea Celery. Revisa el retorno de process_and_store_file.")
            # ---------------------------


            return jsonify({"message": "File uploaded and processed", "file_info": file_info}), 200
        except Exception as e:
            logging.error(f"Error processing file upload: {e}", exc_info=True)
            return jsonify({"error": "Internal server error during file upload", "details": str(e)}), 500
    return jsonify({"error": "Unknown error"}), 500

@app.route('/vault/download/<file_id>', methods=['GET'])
@jwt_required() # Protege el endpoint de descarga
def download_file(file_id):
    try:
        current_user_id_str = get_jwt_identity() # Obtiene el user_id del token
        user_id_from_token = UUID(current_user_id_str) # Convierte a UUID

        session = request.db_session

        try:
            file_uuid = UUID(file_id)
        except ValueError:
            return jsonify({"error": "Invalid file ID format"}), 400

        # Primero, busca el archivo por su ID
        encrypted_file_entry = session.query(EncryptedFile).filter_by(id=file_uuid).first()

        if not encrypted_file_entry:
            return jsonify({"error": "File not found"}), 404

        # Segundo, VERIFICA que el user_id del archivo coincida con el user_id del token
        if encrypted_file_entry.user_id != user_id_from_token:
            logging.warning(f"Intento de acceso no autorizado a archivo {file_id} por usuario {user_id_from_token}. Propietario: {encrypted_file_entry.user_id}")
            return jsonify({"error": "Unauthorized access: You do not own this file"}), 403 # Prohibido

        file_processor = app.config['FILE_PROCESSOR_SERVICE']
        decrypted_data = file_processor.retrieve_and_decrypt_file(encrypted_file_entry)

        response = make_response(decrypted_data)
        response.headers.set('Content-Type', encrypted_file_entry.mimetype)
        response.headers.set('Content-Disposition', 'attachment', filename=encrypted_file_entry.original_filename)
        return response

    except Exception as e:
        logging.error(f"Error downloading file {file_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error during file download", "details": str(e)}), 500

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

@app.route('/')
def home():
    return "Digital Vault Project API is running!"


# --- Nueva Ruta para Consultas RAG ---
@app.route('/ask', methods=['POST'])
@jwt_required() # Protege este endpoint, asumiendo que las consultas son de usuarios autenticados
def ask_question():
    user_question = request.json.get('question')
    if not user_question:
        return jsonify({"error": "No se proporcionó ninguna pregunta."}), 400

    # Opcional: Filtra por user_id si quieres que la búsqueda sea solo sobre los documentos del usuario
    # current_user_id_str = get_jwt_identity()
    # user_id_from_token = UUID(current_user_id_str)

    # 1. Obtener embedding de la pregunta del usuario
    # Asegúrate de que OLLAMA_EMBEDDING_MODEL esté definido y accesible (ej. con os.getenv)
    OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL")
    question_embedding = get_ollama_embedding(user_question, model_name=OLLAMA_EMBEDDING_MODEL)
    if not question_embedding:
        return jsonify({"error": "No se pudo generar el embedding de la pregunta."}), 500

    retrieved_chunks = []
    session = request.db_session # ### CAMBIOS AQUÍ: Usa la sesión de SQLAlchemy

    try:
        # ### CAMBIOS CLAVE AQUÍ: Añadir CAST(:embedding AS vector)
        # Esto le dice a PostgreSQL que el parámetro :embedding debe ser tratado como un tipo VECTOR
        result = session.execute(
            text("SELECT chunk_text FROM document_chunks ORDER BY chunk_embedding <=> CAST(:embedding AS vector) LIMIT 3;"),
            {"embedding": question_embedding} # Pasa el embedding como parámetro nombrado
        )
        retrieved_chunks = [row.chunk_text for row in result.fetchall()] # Accede por nombre de columna

    except Exception as e:
        logging.error(f"Error al buscar en la base de datos: {e}", exc_info=True)
        return jsonify({"error": "Error al buscar información relevante en los documentos."}), 500
    # No es necesario un bloque 'finally' para cerrar la conexión explícitamente
    # ya que SQLAlchemy la maneja con el @app.teardown_request

    if not retrieved_chunks:
        return jsonify({"answer": "No pude encontrar información relevante en los documentos indexados."})

    # 3. Construir el prompt para el modelo de generación
    context = "\n".join(retrieved_chunks)

    # Este prompt guía al LLM para responder solo con el contexto dado
    prompt_for_llm = (
        f"Basado en el siguiente contexto, responde a la pregunta. "
        f"Si la respuesta no se encuentra directamente en el contexto, indica que no tienes suficiente información "
        f"y no intentes inventar la respuesta.\n\n"
        f"Contexto:\n{context}\n\n"
        f"Pregunta: {user_question}\n"
        f"Respuesta:"
    )

    logging.info(f"Enviando prompt al LLM: {prompt_for_llm[:200]}...") # Logear solo una parte del prompt

    # 4. Obtener la respuesta del modelo de generación
    # Asegúrate de que OLLAMA_GENERATION_MODEL está accesible aquí (usualmente vía os.getenv o app.config)
    OLLAMA_GENERATION_MODEL = os.getenv("OLLAMA_GENERATION_MODEL") # O lo cargas de app.config
    llm_response = get_ollama_generation(prompt_for_llm, model_name=OLLAMA_GENERATION_MODEL)

    return jsonify({"answer": llm_response})


# --- Punto de entrada principal ---
if __name__ == '__main__':
    logging.info("Starting Flask app in development mode (if __name__ == '__main__':)")
    app.run(debug=True, host='0.0.0.0', port=5000)
