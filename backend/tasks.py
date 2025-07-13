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

# --- Importar Minio Client (necesario para descargar archivos) ---
from minio import Minio
from minio.error import S3Error

# --- Importaciones para Calibre (ebook-convert) ---
import subprocess # Para ejecutar comandos externos
import tempfile # Para crear archivos temporales

# --- Importaciones para Tesseract OCR ---
import pytesseract # Asegúrate de que 'pytesseract' esté instalado
from PIL import Image # Asegúrate de que 'Pillow' esté instalado

# --- Importaciones para ClamAV ---
# subprocess, tempfile, os ya deberían estar importados, pero asegúrate.


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

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DB_USER = os.getenv("POSTGRES_USER", "dvu")
    DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "secret")
    DB_NAME = os.getenv("POSTGRES_DB", "digital_vault_db")
    DB_HOST = os.getenv("POSTGRES_HOST", "postgres_db")
    DB_PORT = os.getenv("POSTGRES_PORT", "5432")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

SYSTEM_MASTER_KEY = os.getenv('DOCUMENT_ENCRYPTION_KEY')
if not SYSTEM_MASTER_KEY:
    raise ValueError("DOCUMENT_ENCRYPTION_KEY no está configurada en las variables de entorno.")

OLLAMA_API_BASE_URL = os.getenv("OLLAMA_API_BASE_URL", "http://ollama:11434")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
OLLAMA_GENERATION_MODEL = os.getenv("OLLAMA_GENERATION_MODEL", "phi3:3.8b-mini-4k-instruct-q4_K_M")
OLLAMA_GENERATION_TIMEOUT = int(os.getenv("OLLAMA_GENERATION_TIMEOUT", "600")) # En segundos (10 minutos por defecto)

# --- Funciones auxiliares (mantén estas aquí o impórtalas si las tienes en otro archivo) ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def get_s3_client():
    return Minio(
        CEPH_ENDPOINT_URL.replace("http://", "").replace("https://", ""),
        access_key=CEPH_ACCESS_KEY,
        secret_key=CEPH_SECRET_KEY,
        secure=CEPH_ENDPOINT_URL.startswith("https://")
    )

def encrypt(data: bytes, key: bytes) -> bytes:
    f = Fernet(key)
    return f.encrypt(data)

def decrypt(data: bytes, key: bytes) -> bytes:
    f = Fernet(key)
    return f.decrypt(data)

def get_ollama_embedding(text: str, model_name: str):
    headers = {'Content-Type': 'application/json'}
    data = {
        "model": model_name,
        "prompt": text
    }
    try:
        response = requests.post(f"{OLLAMA_API_BASE_URL}/api/embeddings", headers=headers, json=data, timeout=OLLAMA_GENERATION_TIMEOUT)
        response.raise_for_status()
        return response.json()['embedding']
    except requests.exceptions.Timeout as e:
        logging.error(f"Tiempo de espera agotado al obtener embedding de Ollama en {OLLAMA_API_BASE_URL}/api/embeddings: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al comunicarse con Ollama para embedding en {OLLAMA_API_BASE_URL}/api/embeddings: {e}")
        raise
    except Exception as e:
        logging.error(f"Error inesperado al obtener embedding de Ollama: {e}")
        raise

def get_ollama_generation(prompt: str, model_name: str):
    headers = {'Content-Type': 'application/json'}
    data = {
        "model": model_name,
        "prompt": prompt,
        "stream": False
    }
    try:
        logging.info(f"Solicitando generación para el modelo '{model_name}' (prompt: {prompt[:100]}...) con timeout {OLLAMA_GENERATION_TIMEOUT}s")
        response = requests.post(f"{OLLAMA_API_BASE_URL}/api/generate", headers=headers, json=data, timeout=OLLAMA_GENERATION_TIMEOUT)
        response.raise_for_status()
        return response.json()['response']
    except requests.exceptions.Timeout as e:
        logging.error(f"Tiempo de espera agotado al generar respuesta de Ollama en {OLLAMA_API_BASE_URL}/api/generate: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al comunicarse con Ollama en {OLLAMA_API_BASE_URL}/api/generate: {e}")
        raise
    except Exception as e:
        logging.error(f"Error inesperado al obtener generación de Ollama: {e}")
        raise


def extract_text_from_file_content(file_content_bytes: bytes, filename: str) -> str:
    """
    Extrae texto de diferentes tipos de contenido de archivo.
    """
    _, file_extension = os.path.splitext(filename)
    file_extension = file_extension.lower()

    if file_extension == '.pdf':
        try:
            reader = PdfReader(io.BytesIO(file_content_bytes))
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text
        except Exception as e:
            logging.error(f"Error al extraer texto de PDF {filename}: {e}", exc_info=True)
            raise
    elif file_extension == '.txt':
        return file_content_bytes.decode('utf-8')
    elif file_extension == '.mobi':
        try:
            # La librería 'mobi' puede ser sensible a la codificación
            from mobi import Mobi
            mobi_book = Mobi(io.BytesIO(file_content_bytes))
            mobi_book.parse()
            text_content = ""
            # mobi_book.contents puede tener capítulos o secciones
            for chapter in mobi_book.contents:
                text_content += chapter.content.decode('utf-8', errors='ignore') + "\n"
            return text_content
        except Exception as e:
            logging.error(f"Error al extraer texto de MOBI {filename}: {e}", exc_info=True)
            raise
    elif file_extension == '.docx':
        try:
            from docx import Document
            document = Document(io.BytesIO(file_content_bytes))
            text = '\n'.join([paragraph.text for paragraph in document.paragraphs])
            return text
        except Exception as e:
            logging.error(f"Error al extraer texto de DOCX {filename}: {e}", exc_info=True)
            raise
    elif file_extension == '.xlsx':
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(file_content_bytes))
            text = []
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                text.append(f"--- Hoja: {sheet_name} ---")
                for row in sheet.iter_rows():
                    row_values = [str(cell.value) if cell.value is not None else "" for cell in row]
                    text.append('\t'.join(row_values))
            return '\n'.join(text)
        except Exception as e:
            logging.error(f"Error al extraer texto de XLSX {filename}: {e}", exc_info=True)
            raise
    elif file_extension == '.pptx':
        try:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(file_content_bytes))
            text_runs = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text_runs.append(shape.text)
            return '\n'.join(text_runs)
        except Exception as e:
            logging.error(f"Error al extraer texto de PPTX {filename}: {e}", exc_info=True)
            raise
    elif file_extension == '.epub':
        try:
            from ebooklib import epub
            import html2text # Necesario para convertir HTML de EPUB a texto plano
            book = epub.read_epub(io.BytesIO(file_content_bytes))
            text_content = []
            for item in book.get_items():
                if item.get_type() == epub.ITEM_DOCUMENT:
                    # Convertir el contenido HTML a texto plano
                    text_content.append(html2text.html2text(item.get_content().decode('utf-8', errors='ignore')))
            return '\n'.join(text_content)
        except Exception as e:
            logging.error(f"Error al extraer texto de EPUB {filename}: {e}", exc_info=True)
            raise
    elif file_extension == '.azw3':
        # Usar Calibre (ebook-convert) para extraer texto de AZW3
        temp_input_azw3_path = None
        temp_output_txt_path = None
        try:
            # Escribir el contenido binario a un archivo temporal AZW3
            with tempfile.NamedTemporaryFile(delete=False, suffix='.azw3', dir='/tmp') as temp_input:
                temp_input.write(file_content_bytes)
                temp_input_azw3_path = temp_input.name

            # Crear un archivo temporal para la salida de texto
            with tempfile.NamedTemporaryFile(delete=False, suffix='.txt', dir='/tmp') as temp_output:
                temp_output_txt_path = temp_output.name

            # Comando para convertir AZW3 a TXT usando ebook-convert
            command = ["ebook-convert", temp_input_azw3_path, temp_output_txt_path]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            
            if result.returncode != 0:
                logging.error(f"ebook-convert falló para {filename}. Stderr: {result.stderr}")
                raise Exception(f"ebook-convert failed: {result.stderr}")

            # Leer el texto del archivo de salida
            with open(temp_output_txt_path, 'r', encoding='utf-8') as f:
                text_content = f.read()
            return text_content

        except FileNotFoundError:
            logging.error("ebook-convert (Calibre) no encontrado. Asegúrate de que esté instalado en el contenedor.")
            raise
        except subprocess.CalledProcessError as e:
            logging.error(f"Error en ebook-convert para {filename}: {e}. Salida: {e.stdout}. Error: {e.stderr}", exc_info=True)
            raise
        except Exception as e:
            logging.error(f"Error al extraer texto de AZW3 {filename}: {e}", exc_info=True)
            raise
        finally:
            # Limpiar archivos temporales
            if temp_input_azw3_path and os.path.exists(temp_input_azw3_path):
                os.remove(temp_input_azw3_path)
            if temp_output_txt_path and os.path.exists(temp_output_txt_path):
                os.remove(temp_output_txt_path)
    elif file_extension in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff']:
        try:
            image = Image.open(io.BytesIO(file_content_bytes))
            text = pytesseract.image_to_string(image, lang='spa+eng')
            return text.strip()
        except pytesseract.TesseractNotFoundError:
            logging.error("Tesseract OCR no encontrado. Asegúrate de que esté instalado en el sistema y en el PATH.")
            raise
        except Exception as e:
            logging.error(f"Error al realizar OCR en la imagen {filename}: {e}", exc_info=True)
            raise
    else:
        logging.warning(f"Tipo de archivo no soportado para extracción de texto: {filename}")
        return f"Contenido del archivo {filename} (tipo no soportado para extracción)."


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> list[str]:
    """Divide el texto en chunks con solapamiento."""
    chunks = []
    if not text:
        return chunks

    start_index = 0
    while start_index < len(text):
        end_index = min(start_index + chunk_size, len(text))
        chunks.append(text[start_index:end_index])
        start_index += (chunk_size - overlap)
        if start_index >= len(text): # Evitar que el último chunk se solape y cree un bucle infinito si el overlap es muy grande
             break
    return chunks

# --- ¡NUEVA FUNCIÓN! Escanea un archivo con ClamAV ---
def scan_file_with_clamav(file_path: str) -> dict:
    """
    Escanea un archivo dado con ClamAV.

    Args:
        file_path (str): La ruta al archivo temporal que se va a escanear.

    Returns:
        dict: Un diccionario con 'status' ('clean', 'infected', 'error') y 'details'.
    """
    try:
        command = ["clamscan", "--no-summary", "--stdout", file_path]
        result = subprocess.run(command, capture_output=True, text=True, check=False)

        if result.returncode == 0:
            logging.info(f"ClamAV: Archivo {file_path} escaneado. ¡Limpio!")
            return {"status": "clean", "details": "No se encontraron amenazas."}
        elif result.returncode == 1:
            infection_details = result.stdout.strip()
            logging.warning(f"ClamAV: ¡AMENAZA DETECTADA! Archivo {file_path}. Detalles: {infection_details}")
            return {"status": "infected", "details": infection_details}
        else:
            error_message = result.stderr.strip() if result.stderr else result.stdout.strip()
            logging.error(f"ClamAV: Error al escanear el archivo {file_path}. Código de salida: {result.returncode}. Mensaje: {error_message}")
            return {"status": "error", "details": f"Error de escaneo: {error_message}"}
    except FileNotFoundError:
        logging.error("ClamAV no encontrado. Asegúrate de que 'clamscan' esté instalado y en el PATH del contenedor.")
        return {"status": "error", "details": "ClamAV no instalado o no accesible."}
    except Exception as e:
        logging.error(f"Error inesperado al ejecutar ClamAV para {file_path}: {e}", exc_info=True)
        return {"status": "error", "details": f"Excepción inesperada durante el escaneo: {e}"}

# --- Tarea Celery para indexación RAG (mantenerla separada) ---
@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def index_document_for_rag(self, file_id_str: str, ceph_path: str):
    """
    Tarea Celery para extraer texto, generar embeddings e indexar
    documentos en la base de datos vectorial para RAG.
    Se llama después de que el archivo ha sido escaneado por virus.
    """
    logging.info(f"RAG: Iniciando indexación para file_id: {file_id_str}")
    conn = None
    cur = None
    minio_client = None
    temp_file_path = None # Usado para Calibre/OCR, se mantiene aquí por si lo necesita internamente la función extract_text_from_file_content

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        minio_client = get_s3_client()
        fernet_master = Fernet(SYSTEM_MASTER_KEY.encode('utf-8'))

        # 1. Recuperar metadatos del archivo y su clave de encriptación
        cur.execute(
            "SELECT original_filename, encryption_key_encrypted FROM files WHERE id = %s",
            (file_id_str,)
        )
        file_record = cur.fetchone()
        if not file_record:
            logging.error(f"RAG: Archivo no encontrado en DB para indexación {file_id_str}.")
            raise ValueError("File record not found for RAG indexing.")

        original_filename, encryption_key_encrypted = file_record

        # Asegurarse de que sea de tipo 'bytes', convirtiendo si es necesario
        if not isinstance(encryption_key_encrypted, bytes):
            encryption_key_encrypted = bytes(encryption_key_encrypted)

        # 2. Descargar y desencriptar el archivo (ya se hizo en process_uploaded_file, pero se repite para que esta tarea sea autónoma si es necesario,
        # aunque en el flujo actual, solo se llamaría si ya está procesado y escaneado limpio.)
        # Considera pasar el 'decrypted_content' directamente si quieres evitar doble descarga/desencriptación,
        # pero esto complicaría el manejo de reintentos de Celery.
        response = minio_client.get_object(CEPH_BUCKET_NAME, ceph_path)
        encrypted_content = response.read()
        response.close()
        response.release_conn() # Libera la conexión
        logging.info(f"RAG: Archivo encriptado '{ceph_path}' descargado para indexación.")

        file_encryption_key = fernet_master.decrypt(encryption_key_encrypted)
        file_fernet = Fernet(file_encryption_key)
        decrypted_content = file_fernet.decrypt(encrypted_content)
        logging.info(f"RAG: Archivo '{original_filename}' desencriptado para indexación.")


        # 3. Extraer el texto del archivo
        logging.info(f"RAG: Extrayendo texto de {original_filename}...")
        text_content = extract_text_from_file_content(decrypted_content, original_filename)
        
        if not text_content or text_content.strip() == "":
            logging.warning(f"RAG: No se pudo extraer texto significativo de {original_filename}. Marcando como 'no_text_extracted'.")
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('no_text_extracted', datetime.now(), file_id_str)
            )
            conn.commit()
            return # No hay texto para indexar

        logging.info(f"RAG: Texto extraído (primeros 200 chars): {text_content[:200]}...")


        # 4. Dividir el texto en chunks y generar embeddings
        chunks = chunk_text(text_content)
        logging.info(f"RAG: Texto dividido en {len(chunks)} chunks.")

        # Eliminar chunks existentes para este archivo antes de insertar nuevos
        cur.execute("DELETE FROM document_chunks WHERE file_id = %s", (file_id_str,))
        conn.commit()
        logging.info(f"RAG: Chunks antiguos eliminados para file_id {file_id_str}.")

        for i, chunk in enumerate(chunks):
            # Asegúrate de que OLLAMA_EMBEDDING_MODEL esté accesible (usualmente vía os.getenv)
            embedding = get_ollama_embedding(chunk, model_name=OLLAMA_EMBEDDING_MODEL)
            # Insertar chunk y embedding en la tabla document_chunks
            cur.execute(
                "INSERT INTO document_chunks (id, file_id, chunk_text, chunk_embedding, chunk_order) VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4(), file_id_str, chunk, embedding, i)
            )
        conn.commit()
        logging.info(f"RAG: Documento {file_id_str} indexado exitosamente en la DB vectorial.")

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
            self.retry(exc=e, countdown=self.default_retry_delay)
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


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_uploaded_file(self, file_id_str: str, ceph_path: str, original_filename: str):
    """
    Tarea Celery para descargar, desencriptar, escanear virus,
    y luego disparar la tarea de indexación RAG si el archivo está limpio.
    """
    logging.info(f"Tarea Celery: Iniciando procesamiento para file_id: {file_id_str}, filename: {original_filename}")
    conn = None
    cur = None
    minio_client = None
    temp_file_path = None # Variable para almacenar la ruta del archivo temporal para ClamAV

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        minio_client = get_s3_client()
        fernet_master = Fernet(SYSTEM_MASTER_KEY.encode('utf-8'))

        # 1. Recuperar metadatos del archivo y su clave de encriptación
        cur.execute(
            "SELECT encryption_key_encrypted, mimetype FROM files WHERE id = %s",
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

        encryption_key_encrypted, mimetype = file_record

        if encryption_key_encrypted is None:
            logging.error(f"Tarea Celery: Clave de encriptación para el archivo {file_id_str} es NULL en la base de datos. No se puede desencriptar.")
            raise ValueError("Encryption key is missing from database.")
        
        if not isinstance(encryption_key_encrypted, bytes):
            try:
                encryption_key_encrypted = bytes(encryption_key_encrypted)
            except TypeError as e:
                logging.error(f"Tarea Celery: La clave de encriptación para el archivo {file_id_str} no es de un tipo convertible a bytes. Error: {e}", exc_info=True)
                raise TypeError("Encryption key is not in a convertible format (expected bytes or similar).") from e

        # 2. Descargar y desencriptar el archivo
        try:
            response = minio_client.get_object(CEPH_BUCKET_NAME, ceph_path)
            encrypted_content = response.read()
            response.close()
            response.release_conn() # Libera la conexión
            logging.info(f"Tarea Celery: Archivo encriptado '{ceph_path}' descargado de Minio.")
        except S3Error as e:
            logging.error(f"Tarea Celery: Error de Minio (S3) al descargar '{ceph_path}': {e}", exc_info=True)
            # Actualizar estado a 'failed_download'
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('failed_download', datetime.now(), file_id_str)
            )
            conn.commit()
            raise # Relanzar para reintento de Celery
        except Exception as e:
            logging.error(f"Tarea Celery: Error inesperado al descargar '{ceph_path}': {e}", exc_info=True)
            # Actualizar estado a 'failed_download'
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('failed_download', datetime.now(), file_id_str)
            )
            conn.commit()
            raise

        try:
            file_encryption_key = fernet_master.decrypt(encryption_key_encrypted)
            file_fernet = Fernet(file_encryption_key)
            decrypted_content = file_fernet.decrypt(encrypted_content)
            logging.info(f"Tarea Celery: Archivo '{original_filename}' desencriptado exitosamente.")
        except Exception as e:
            logging.error(f"Tarea Celery: Error al desencriptar el archivo '{original_filename}': {e}", exc_info=True)
            # Actualizar estado a 'failed_decryption'
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('failed_decryption', datetime.now(), file_id_str)
            )
            conn.commit()
            raise

        # --- ¡NUEVO! PASO 3: Escanear el archivo en busca de virus con ClamAV real ---
        logging.info(f"Tarea Celery: Escaneando archivo {original_filename} (file_id: {file_id_str}) en busca de virus...")
        
        # Guardar el contenido desencriptado en un archivo temporal para que ClamAV lo pueda escanear
        with tempfile.NamedTemporaryFile(delete=False, dir='/tmp') as temp_file:
            temp_file.write(decrypted_content)
            temp_file_path = temp_file.name # Obtener la ruta del archivo temporal

        scan_result = scan_file_with_clamav(temp_file_path) # Llamar a la función de escaneo real

        if scan_result["status"] == "infected":
            logging.error(f"Tarea Celery: Archivo {original_filename} (file_id: {file_id_str}) INFECTADO. Detalles: {scan_result['details']}")
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('infected', datetime.now(), file_id_str)
            )
            conn.commit()
            # No se reintenta si está infectado, se marca y se termina.
            return # Terminar la tarea aquí

        elif scan_result["status"] == "error":
            logging.error(f"Tarea Celery: Error al escanear el archivo {original_filename} (file_id: {file_id_str}). Detalles: {scan_result['details']}")
            cur.execute(
                "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                ('scan_failed', datetime.now(), file_id_str)
            )
            conn.commit()
            self.retry(exc=Exception(scan_result['details']), countdown=self.default_retry_delay)
            return # Reintentar si hubo un error en el escaneo

        # Si el estado es 'clean', se continúa el procesamiento
        logging.info(f"Tarea Celery: Archivo {original_filename} (file_id: {file_id_str}) limpio, continuando con la indexación RAG.")
        cur.execute(
            "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
            ('scanned_clean', datetime.now(), file_id_str) # Nuevo estado: 'scanned_clean'
        )
        conn.commit()

        # --- PASO 4: Disparar la tarea de indexación RAG si el archivo está limpio ---
        index_document_for_rag.delay(file_id_str, ceph_path) # Pasa ceph_path si index_document_for_rag lo necesita para redescargar
        logging.info(f"Tarea Celery: Disparada tarea de indexación RAG para {file_id_str}.")
        # -----------------------------------------------------------

    except Exception as e:
        logging.error(f"Tarea Celery: Error general en el procesamiento de archivo {file_id_str}: {e}", exc_info=True)
        if conn:
            conn.rollback()
        try:
            self.retry(exc=e, countdown=self.default_retry_delay)
        except self.MaxRetriesExceededError:
            logging.error(f"Tarea Celery: Se excedieron los reintentos para file_id {file_id_str}. Marcando como fallo crítico.")
            try:
                if conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE files SET processed_status = %s, last_processed_at = %s WHERE id = %s",
                        ('failed_processing', datetime.now(), file_id_str)
                    )
                    conn.commit()
            except Exception as db_e:
                logging.error(f"Tarea Celery: Error al actualizar estado de fallo crítico en DB para {file_id_str}: {db_e}")
    finally:
        # ¡IMPORTANTE! Limpiar el archivo temporal creado para ClamAV
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            logging.info(f"Archivo temporal {temp_file_path} eliminado.")
        if conn:
            conn.close()