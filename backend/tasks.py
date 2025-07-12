# backend/tasks.py
import os
from celery import Celery
import psycopg2
import json
from cryptography.fernet import Fernet
import time
import logging
from uuid import UUID  # Para manejar UUIDs en la base de datos
from datetime import datetime # Import datetime for upload_timestamp or last_processed_at

# --- Para extracción de texto de PDFs ---
from pypdf import PdfReader  # pip install pypdf
import io # Necesario para io.BytesIO con PdfReader

# --- Para interactuar con Ollama y otras APIs HTTP ---
import requests  # pip install requests
from requests.exceptions import ConnectionError, Timeout, RequestException # Importaciones para manejo de errores de requests

# --- Importar Minio Client (necesario para descargar archivos) ---
from minio import Minio
from minio.error import S3Error

import docx # Para .docx (python-docx)
import openpyxl # Para .xlsx
from pptx import Presentation # Para .pptx (python-pptx)
import ebooklib # Para .epub
from ebooklib import epub
import mobi # pip install mobi

# --- Importaciones para Tesseract OCR ---
import pytesseract # Asegúrate de que 'pytesseract' esté instalado
from PIL import Image # Asegúrate de que 'Pillow' esté instalado

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuración de Celery ---
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

celery_app = Celery('tasks', broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)

# --- Configuración para tareas (variables de entorno) ---
CEPH_ENDPOINT_URL = os.getenv('CEPH_ENDPOINT_URL')
CEPH_ACCESS_KEY = os.getenv('CEPH_ACCESS_KEY')
CEPH_SECRET_KEY = os.getenv('CEPH_SECRET_KEY')
CEPH_BUCKET_NAME = os.getenv('CEPH_BUCKET_NAME')
POSTGRES_DB = os.getenv('POSTGRES_DB')
POSTGRES_USER = os.getenv('POSTGRES_USER')
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD')
POSTGRES_HOST = os.getenv('POSTGRES_HOST')
POSTGRES_PORT = os.getenv('POSTGRES_PORT', '5432')
SYSTEM_MASTER_KEY = os.getenv('SYSTEM_MASTER_KEY') # Asegúrate de que esta clave sea accesible para Celery

# --- Configuración de Ollama (ajusta la URL según tu instalación) ---
# Usamos 'http://ollama:11434' porque 'ollama' es el nombre del servicio en docker-compose
OLLAMA_API_BASE_URL = os.getenv('OLLAMA_API_BASE_URL', 'http://ollama:11434')
OLLAMA_EMBEDDING_MODEL = os.getenv('OLLAMA_EMBEDDING_MODEL', 'nomic-embed-text') # Modelo para embeddings
OLLAMA_GENERATION_MODEL = os.getenv('OLLAMA_GENERATION_MODEL', 'llama3') # Modelo para generación de texto

# --- Funciones Auxiliares para Conexión a DB y S3 ---
def get_db_connection():
    """Establece y retorna una conexión a la base de datos PostgreSQL."""
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD
        )
        return conn
    except Exception as e:
        logging.error(f"Error al conectar a la base de datos: {e}", exc_info=True)
        raise

def get_s3_client():
    """Inicializa y retorna un cliente MinIO (S3)."""
    try:
        minio_host = CEPH_ENDPOINT_URL.replace("http://", "").replace("https://", "")
        secure_connection = CEPH_ENDPOINT_URL.startswith("https://")
        
        client = Minio(
            minio_host,
            access_key=CEPH_ACCESS_KEY,
            secret_key=CEPH_SECRET_KEY,
            secure=secure_connection
        )
        return client
    except Exception as e:
        logging.error(f"Error al inicializar cliente S3: {e}", exc_info=True)
        raise

# --- Funciones para RAG e Indexación ---
def get_ollama_embedding(text: str, model_name: str = OLLAMA_EMBEDDING_MODEL):
    """
    Obtiene el embedding de un texto usando el modelo de embedding de Ollama.
    """
    url = f"{OLLAMA_API_BASE_URL}/api/embeddings"
    headers = {"Content-Type": "application/json"}
    data = {
        "model": model_name,
        "prompt": text
    }
    try:
        logging.info(f"Solicitando embedding para el modelo '{model_name}' (texto: {text[:50]}...)")
        response = requests.post(url, headers=headers, json=data, timeout=60) # Añade timeout
        response.raise_for_status() # Lanza excepción para códigos de estado HTTP 4xx/5xx
        result = response.json()
        embedding = result.get('embedding')
        if not embedding:
            logging.error(f"La respuesta de Ollama no contiene 'embedding': {result}")
            return None
        logging.info(f"Embedding obtenido, tamaño: {len(embedding)}")
        return embedding
    except ConnectionError as e:
        logging.error(f"Error de conexión a Ollama API en {url}: {e}")
        return None
    except Timeout as e:
        logging.error(f"Tiempo de espera agotado al conectar con Ollama API en {url}: {e}")
        return None
    except RequestException as e:
        logging.error(f"Error al obtener embedding de Ollama ({response.status_code if 'response' in locals() else 'N/A'}): {e}", exc_info=True)
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Error al decodificar JSON de la respuesta de Ollama: {e}. Respuesta: {response.text}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"Error inesperado al obtener embedding de Ollama: {e}", exc_info=True)
        return None

def get_ollama_generation(prompt: str, model_name: str = OLLAMA_GENERATION_MODEL):
    """
    Obtiene una generación de texto del modelo Ollama.
    """
    url = f"{OLLAMA_API_BASE_URL}/api/generate"
    headers = {"Content-Type": "application/json"}
    data = {
        "model": model_name,
        "prompt": prompt,
        "stream": False # No queremos streaming para este caso
    }
    try:
        logging.info(f"Solicitando generación para el modelo '{model_name}' (prompt: {prompt[:100]}...)")
        response = requests.post(url, headers=headers, json=data, timeout=1200) # Un timeout más largo para generación
        response.raise_for_status()
        result = response.json()
        generated_text = result.get('response')
        if not generated_text:
            logging.error(f"La respuesta de Ollama no contiene 'response': {result}")
            return "Lo siento, no pude generar una respuesta."
        logging.info("Generación de Ollama exitosa.")
        return generated_text
    except ConnectionError as e:
        logging.error(f"Error de conexión a Ollama API para generación en {url}: {e}")
        return "Lo siento, no pude conectar con el servicio de IA."
    except Timeout as e:
        logging.error(f"Tiempo de espera agotado al generar respuesta de Ollama en {url}: {e}")
        return "Lo siento, la IA tardó demasiado en responder."
    except RequestException as e:
        logging.error(f"Error al generar respuesta de Ollama ({response.status_code if 'response' in locals() else 'N/A'}): {e}", exc_info=True)
        return "Lo siento, ocurrió un error al generar la respuesta."
    except json.JSONDecodeError as e:
        logging.error(f"Error al decodificar JSON de la respuesta de Ollama para generación: {e}. Respuesta: {response.text}", exc_info=True)
        return "Lo siento, la respuesta de la IA no es válida."
    except Exception as e:
        logging.error(f"Error inesperado al generar respuesta de Ollama: {e}", exc_info=True)
        return "Lo siento, ocurrió un error inesperado al procesar tu solicitud."

def extract_text_from_pdf(pdf_content_bytes):
    """    Extrae texto de un archivo PDF. """
    try:
        reader = PdfReader(io.BytesIO(pdf_content_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or "" # Manejar páginas vacías
        return text
    except Exception as e:
        logging.error(f"Error al extraer texto de PDF: {e}", exc_info=True)
        return None
    """    Extrae texto de diferentes tipos de contenido de archivo. """
    _, file_extension = os.path.splitext(filename)
    file_extension = file_extension.lower() # Convertir a minúsculas para consistencia

    if file_extension == '.pdf':
        try:
            reader = PdfReader(io.BytesIO(file_content_bytes))
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text
        except Exception as e:
            logging.error(f"Error al extraer texto de PDF: {e}")
            raise

    elif file_extension == '.txt':
        try:
            # Intentar decodificar como UTF-8, si falla, intentar con ISO-8859-1 o chardet
            return file_content_bytes.decode('utf-8')
        except UnicodeDecodeError:
            return file_content_bytes.decode('latin-1', errors='ignore') # Una alternativa
        except Exception as e:
            logging.error(f"Error al extraer texto de TXT: {e}")
            raise

    elif file_extension == '.mobi':
        try:
            mobi_book = mobi.read(io.BytesIO(file_content_bytes))
            return b"".join(mobi_book.text).decode('utf-8', errors='ignore')
        except Exception as e:
            logging.error(f"Error al extraer texto de MOBI: {e}")
            raise

    # --- ¡NUEVA LÓGICA PARA DOCX! ---
    elif file_extension == '.docx':
        try:
            document = docx.Document(io.BytesIO(file_content_bytes))
            full_text = []
            for paragraph in document.paragraphs:
                full_text.append(paragraph.text)
            return "\n".join(full_text)
        except Exception as e:
            logging.error(f"Error al extraer texto de DOCX: {e}")
            raise

    # --- ¡NUEVA LÓGICA PARA XLSX! ---
    elif file_extension == '.xlsx':
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(file_content_bytes))
            full_text = []
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                full_text.append(f"--- Hoja: {sheet_name} ---")
                for row in sheet.iter_rows():
                    row_values = [str(cell.value) if cell.value is not None else "" for cell in row]
                    full_text.append("\t".join(row_values)) # Unir celdas con tabulador
            return "\n".join(full_text)
        except Exception as e:
            logging.error(f"Error al extraer texto de XLSX: {e}")
            raise

    # --- ¡NUEVA LÓGICA PARA PPTX! ---
    elif file_extension == '.pptx':
        try:
            prs = Presentation(io.BytesIO(file_content_bytes))
            full_text = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        full_text.append(shape.text)
            return "\n".join(full_text)
        except Exception as e:
            logging.error(f"Error al extraer texto de PPTX: {e}")
            raise

    # --- ¡NUEVA LÓGICA PARA EPUB! ---
    elif file_extension == '.epub':
        try:
            book = epub.read_epub(io.BytesIO(file_content_bytes))
            full_text = []
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    # Intenta extraer texto de contenido HTML
                    import html2text # Necesitarías instalar esto también: pip install html2text
                    text_content = html2text.html2text(item.get_content().decode('utf-8', errors='ignore'))
                    full_text.append(text_content)
            return "\n".join(full_text)
        except Exception as e:
            logging.error(f"Error al extraer texto de EPUB: {e}")
            raise

    # --- ¡NUEVA LÓGICA PARA AZW3 USANDO ebook-convert! ---
    elif file_extension == '.azw3':
        text_content = ""
        temp_input_path = None
        temp_output_path = None
        try:
            # 1. Guardar el contenido binario del archivo .azw3 en un archivo temporal
            with tempfile.NamedTemporaryFile(delete=False, suffix=".azw3") as temp_input:
                temp_input.write(file_content_bytes)
                temp_input_path = temp_input.name # Guardar la ruta del archivo temporal

            # 2. Crear un archivo temporal para la salida de texto plano
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as temp_output:
                temp_output_path = temp_output.name # Guardar la ruta del archivo temporal

            # 3. Llamar a ebook-convert para convertir el .azw3 a .txt
            # Es recomendable usar 'xvfb-run' si Calibre necesita un servidor X (interfaz gráfica)
            # en un entorno sin cabeza (Docker), aunque 'ebook-convert' a veces funciona sin él.
            command = ["ebook-convert", temp_input_path, temp_output_path]
            
            # Ejecutar el comando. 'check=True' lanzará una excepción si el comando falla.
            # 'capture_output=True' capturará stdout y stderr.
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            
            if result.stderr:
                logging.warning(f"ebook-convert produjo advertencias/errores en stderr: {result.stderr}")

            # 4. Leer el contenido del archivo de texto convertido
            with open(temp_output_path, 'r', encoding='utf-8') as f:
                text_content = f.read()
            
            return text_content

        except subprocess.CalledProcessError as e:
            logging.error(f"Error al convertir AZW3 con ebook-convert: {e.cmd} - {e.returncode} - {e.stderr}", exc_info=True)
            # Puedes retornar un mensaje de error o levantar la excepción
            raise
        except Exception as e:
            logging.error(f"Error general al extraer texto de AZW3: {e}", exc_info=True)
            raise
        finally:
            # 5. Limpiar archivos temporales, ¡esto es crucial!
            if temp_input_path and os.path.exists(temp_input_path):
                os.remove(temp_input_path)
            if temp_output_path and os.path.exists(temp_output_path):
                os.remove(temp_output_path)

    # --- ¡NUEVA LÓGICA PARA IMÁGENES (OCR)! ---
    elif file_extension in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff']:
        try:
            # Abrir la imagen desde los bytes
            image = Image.open(io.BytesIO(file_content_bytes))
            
            # Realizar OCR en la imagen. Puedes especificar múltiples idiomas.
            # Asegúrate de que los idiomas 'spa' y 'eng' (o los que uses) estén instalados en Tesseract OCR.
            text = pytesseract.image_to_string(image, lang='spa+eng')
            
            # Opcional: Limpiar el texto si hay muchos saltos de línea o espacios en blanco excesivos
            return text.strip()
        except pytesseract.TesseractNotFoundError:
            logging.error("Tesseract OCR no encontrado. Asegúrate de que esté instalado en el sistema y en el PATH.")
            raise
        except Exception as e:
            logging.error(f"Error al realizar OCR en la imagen {filename}: {e}", exc_info=True)
            raise

    else:
        logging.warning(f"Tipo de archivo no soportado para extracción de texto: {filename}")
        return f"Contenido del archivo {filename} (tipo no soportado para extracción)." # O ""


def chunk_text(text, chunk_size=500, chunk_overlap=50):
    """Divide el texto en chunks con solapamiento."""
    chunks = []
    # Usar una división por espacio para evitar cortar palabras por la mitad
    words = text.split() 
    
    if len(words) <= chunk_size:
        return [text] # Si el texto es menor que el tamaño del chunk, no lo dividas

    for i in range(0, len(words), chunk_size - chunk_overlap):
        chunk = " ".join(words[i : i + chunk_size])
        chunks.append(chunk)
    return chunks

# --- Tareas Celery ---

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_uploaded_file(self, file_id_str, ceph_path):
    """
    Tarea Celery para descargar, desencriptar, simular escaneo de virus
    y actualizar el estado del archivo.
    """
    logging.info(f"Tarea Celery: Iniciando procesamiento para file_id: {file_id_str}")
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        s3 = get_s3_client()
        fernet_master = Fernet(SYSTEM_MASTER_KEY.encode('utf-8'))

        # 1. Recuperar metadatos del archivo y su clave de encriptación
        cur.execute(
            "SELECT original_filename, encryption_key_encrypted, mimetype FROM files WHERE id = %s",
            (file_id_str,)
        )
        file_record = cur.fetchone()
        if not file_record:
            logging.error(f"Tarea Celery: Archivo no encontrado en DB para file_id {file_id_str}. No se puede procesar.")
            # Actualizar estado a 'failed_processing' si el archivo no existe
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                        ('failed_processing', datetime.now(), file_id_str)
                    )
                    conn.commit()
                except Exception as db_e:
                    logging.error(f"Tarea Celery: Error al actualizar estado de fallo en DB para {file_id_str} al no encontrar archivo: {db_e}")
            return

        original_filename, encryption_key_encrypted, mimetype = file_record

        # --- FIX 1: Más robusta verificación de la clave de encriptación antes de desencriptar ---
        if encryption_key_encrypted is None:
            logging.error(f"Tarea Celery: Clave de encriptación para el archivo {file_id_str} es NULL en la base de datos. No se puede desencriptar.")
            raise ValueError("Encryption key is missing from database.")
        
        # Asegurarse de que sea de tipo 'bytes', convirtiendo si es necesario (ej. de memoryview)
        if not isinstance(encryption_key_encrypted, bytes):
            try:
                encryption_key_encrypted = bytes(encryption_key_encrypted)
            except TypeError as e:
                logging.error(f"Tarea Celery: La clave de encriptación para el archivo {file_id_str} no es de un tipo convertible a bytes. Error: {e}", exc_info=True)
                raise TypeError("Encryption key is not in a convertible format (expected bytes or similar).") from e
        # --- FIN FIX 1 ---

        # 2. Descargar y desencriptar el archivo
        try:
            response = s3.get_object(CEPH_BUCKET_NAME, ceph_path)
            encrypted_content = response.read()
            response.close()
            response.release_conn() # Libera la conexión
            logging.info(f"Tarea Celery: Archivo encriptado '{ceph_path}' descargado de MinIO.")
        except S3Error as e:
            logging.error(f"Tarea Celery: Error de MinIO (S3) al descargar '{ceph_path}': {e}", exc_info=True)
            raise
        except Exception as e:
            logging.error(f"Tarea Celery: Error inesperado al descargar '{ceph_path}': {e}", exc_info=True)
            raise

        file_encryption_key = fernet_master.decrypt(encryption_key_encrypted)
        file_fernet = Fernet(file_encryption_key)
        decrypted_content = file_fernet.decrypt(encrypted_content)
        logging.info(f"Tarea Celery: Archivo '{original_filename}' desencriptado exitosamente.")

        # 3. Simular escaneo de virus
        virus_found = False
        logging.info(f"Tarea Celery: Simulando escaneo de virus para {original_filename}...")
        time.sleep(2) # Simula el tiempo de escaneo

        if virus_found:
            processed_status = 'infected'
            logging.warning(f"Tarea Celery: Archivo {original_filename} marcado como 'infectado'.")
        else:
            processed_status = 'virus_scanned'
            logging.info(f"Tarea Celery: Archivo {original_filename} escaneado. No se encontraron amenazas.")

        # 4. Actualizar el estado en la base de datos
        cur.execute(
            "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
            (processed_status, datetime.now(), file_id_str)
        )
        conn.commit()
        logging.info(f"Tarea Celery: Estado de procesamiento para {file_id_str} actualizado a '{processed_status}'.")

        # --- ¡Añade esta llamada para iniciar la indexación RAG si el archivo está limpio! ---
        if not virus_found:
            index_document_for_rag.delay(file_id_str, ceph_path)
            logging.info(f"Tarea Celery: Disparada tarea de indexación RAG para {file_id_str}.")
        # -----------------------------------------------------------

    except Exception as e:
        logging.error(f"Tarea Celery: Error al procesar archivo {file_id_str}: {e}", exc_info=True)
        # Manejo de reintentos
        try:
            # --- FIX 2: Correcto acceso a default_retry_delay ---
            self.retry(exc=e, countdown=self.default_retry_delay)
            # --- FIN FIX 2 ---
        except self.MaxRetriesExceededError:
            logging.error(f"Tarea Celery: Se excedieron los reintentos para file_id {file_id_str}. No se pudo procesar.")
            # Actualizar DB con estado de fallo crítico si no se pudo reintentar más
            try:
                if conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                        ('failed_processing', datetime.now(), file_id_str)
                    )
                    conn.commit()
            except Exception as db_e:
                logging.error(f"Tarea Celery: Error al actualizar estado de fallo en DB para {file_id_str}: {db_e}")
    finally:
        if conn:
            conn.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def index_document_for_rag(self, file_id_str, ceph_path):
    """
    Tarea Celery para extraer texto de un documento, chunking, generar embeddings
    y almacenar en la tabla document_chunks para RAG.
    """
    logging.info(f"Tarea Celery RAG: Iniciando indexación para file_id: {file_id_str}")
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        s3 = get_s3_client()
        fernet_master = Fernet(SYSTEM_MASTER_KEY.encode('utf-8'))

        # 1. Recuperar metadatos del archivo y su clave de encriptación
        cur.execute(
            "SELECT original_filename, encryption_key_encrypted, mimetype FROM files WHERE id = %s",
            (file_id_str,)
        )
        file_record = cur.fetchone()
        if not file_record:
            logging.error(f"Tarea Celery RAG: Archivo no encontrado en DB para file_id {file_id_str}. Terminando indexación.")
            # Actualizar estado a 'failed_indexing' si el archivo no existe
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                        ('failed_indexing', datetime.now(), file_id_str)
                    )
                    conn.commit()
                except Exception as db_e:
                    logging.error(f"Tarea Celery RAG: Error al actualizar estado de fallo en DB para {file_id_str} al no encontrar archivo: {db_e}")
            return

        original_filename, encryption_key_encrypted, mimetype = file_record
        
        # --- FIX 1 (también aquí): Más robusta verificación de la clave de encriptación ---
        if encryption_key_encrypted is None:
            logging.error(f"Tarea Celery RAG: Clave de encriptación para el archivo {file_id_str} es NULL en la base de datos. No se puede desencriptar para RAG.")
            raise ValueError("Encryption key is missing from database for RAG indexing.")
        
        if not isinstance(encryption_key_encrypted, bytes):
            try:
                encryption_key_encrypted = bytes(encryption_key_encrypted)
            except TypeError as e:
                logging.error(f"Tarea Celery RAG: La clave de encriptación para el archivo {file_id_str} no es de un tipo convertible a bytes para RAG. Error: {e}", exc_info=True)
                raise TypeError("Encryption key is not in a convertible format (expected bytes or similar) for RAG indexing.") from e
        # --- FIN FIX 1 ---

        # 2. Descargar y desencriptar el archivo
        try:
            response = s3.get_object(CEPH_BUCKET_NAME, ceph_path)
            encrypted_content = response.read()
            response.close()
            response.release_conn()
        except S3Error as e:
            logging.error(f"Error al descargar archivo '{ceph_path}' de MinIO para RAG: {e}", exc_info=True)
            raise
        except Exception as e:
            logging.error(f"Error inesperado al descargar '{ceph_path}' para RAG: {e}", exc_info=True)
            raise

        file_encryption_key = fernet_master.decrypt(encryption_key_encrypted)
        file_fernet = Fernet(file_encryption_key)
        decrypted_content = file_fernet.decrypt(encrypted_content)
        logging.info(f"Tarea Celery RAG: Archivo {file_id_str} descargado y desencriptado para indexación.")

        # 3. Extraer texto basado en el tipo de archivo
        text_content = None
        if mimetype == 'application/pdf' or original_filename.lower().endswith('.pdf'):
            text_content = extract_text_from_pdf(decrypted_content)
        elif mimetype == 'text/plain' or original_filename.lower().endswith('.txt'):
            text_content = decrypted_content.decode('utf-8', errors='ignore')
        
        if not text_content or not text_content.strip():
            logging.warning(f"Tarea Celery RAG: No se pudo extraer texto o el texto está vacío de {original_filename} (file_id: {file_id_str}). Saltando indexación.")
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('no_text_extracted', datetime.now(), file_id_str)
            )
            conn.commit()
            return

        # 4. Chunking del texto
        chunks = chunk_text(text_content)
        logging.info(f"Tarea Celery RAG: Texto chunked en {len(chunks)} partes para {file_id_str}.")

        # 5. Generar embeddings y almacenar en la DB vectorial
        for i, chunk in enumerate(chunks):
            embedding = get_ollama_embedding(chunk)
            if embedding:
                cur.execute(
                    "INSERT INTO document_chunks (file_id, chunk_text, chunk_embedding, chunk_order) VALUES (%s, %s, %s, %s)",
                    (file_id_str, chunk, embedding, i)
                )
            else:
                logging.error(f"Tarea Celery RAG: No se pudo generar embedding para chunk {i} de {file_id_str}.")
        
        conn.commit()
        logging.info(f"Tarea Celery RAG: Documento {file_id_str} indexado exitosamente en la DB vectorial.")

        # Opcional: Actualizar el estado del archivo en `files` a 'indexed'
        cur.execute(
            "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
            ('indexed', datetime.now(), file_id_str)
        )
        conn.commit()

    except Exception as e:
        logging.error(f"Tarea Celery RAG: Error en la indexación para file_id {file_id_str}: {e}", exc_info=True)
        if conn:
            conn.rollback()
        try:
            # --- FIX 2 (también aquí): Correcto acceso a default_retry_delay ---
            self.retry(exc=e, countdown=self.default_retry_delay)
            # --- FIN FIX 2 ---
        except self.MaxRetriesExceededError:
            logging.error(f"Tarea Celery RAG: Se excedieron los reintentos para indexar file_id {file_id_str}. Marcando como fallo crítico.")
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                        ('failed_indexing', datetime.now(), file_id_str)
                    )
                    conn.commit()
                except Exception as db_e:
                    logging.error(f"Tarea Celery RAG: Error al actualizar estado de fallo de indexación en DB para {file_id_str}: {db_e}")
    finally:
        if conn:
            conn.close()
