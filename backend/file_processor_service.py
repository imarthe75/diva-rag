import os
import uuid
import json
import io # Necesario para manejar datos binarios como BytesIO
from datetime import datetime
from cryptography.fernet import Fernet
from minio import Minio
from minio.error import S3Error
from kafka import KafkaProducer
import logging

# Importa tus modelos de SQLAlchemy
# Asegúrate de que models.py esté en la ruta correcta y defina EncryptedFile y User
from models import EncryptedFile, User 

class FileProcessorService:
    def __init__(self, s3_endpoint_url, s3_access_key, s3_secret_key, s3_bucket_name,
                 master_key, kafka_bootstrap_servers, kafka_topic_uploaded):
        
        # --- Configuración de Logging ---
        # Puedes ajustar el nivel de logging basado en una variable de entorno si lo deseas
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

        # --- Configuración de MinIO ---
        # El SDK de MinIO no necesita el prefijo http:// o https://
        minio_host = s3_endpoint_url.replace("http://", "").replace("https://", "")
        # Determina si usar SSL (HTTPS)
        secure_connection = s3_endpoint_url.startswith("https://") 
        
        try:
            self.minio_client = Minio(
                minio_host,
                access_key=s3_access_key,
                secret_key=s3_secret_key,
                secure=secure_connection # Usa True para HTTPS, False para HTTP
            )
            self.s3_bucket_name = s3_bucket_name
            self.logger.info(f"Cliente MinIO inicializado para {s3_endpoint_url}.")
        except Exception as e:
            self.logger.error(f"Error al inicializar el cliente MinIO: {e}")
            raise # Lanza la excepción para asegurar que la app no inicie sin MinIO

        # --- Configuración de Fernet (encriptación simétrica) ---
        try:
            self.fernet_master = Fernet(master_key.encode('utf-8'))
            self.logger.info("Clave maestra de Fernet cargada correctamente.")
        except Exception as e:
            self.logger.error(f"Error al cargar la clave maestra de Fernet: {e}. Asegúrate de que SYSTEM_MASTER_KEY sea una clave Fernet válida en Base64.")
            raise

        # --- Configuración de Kafka ---
        self.kafka_topic_uploaded = kafka_topic_uploaded # <--- ¡Añade ESTA LÍNEA!
        self.kafka_enabled = os.getenv("ENABLE_KAFKA", "False").lower() == "true"
        self.kafka_producer = None
        if self.kafka_enabled:
            try:
                self.kafka_producer = KafkaProducer(
                    bootstrap_servers=kafka_bootstrap_servers.split(','),
                    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                    api_version=(0, 10, 2) # Compatible con versiones recientes de Kafka
                )
                self.logger.info(f"Productor de Kafka inicializado para {kafka_bootstrap_servers}.")
            except Exception as e:
                self.logger.error(f"Error al inicializar el productor de Kafka: {e}. Kafka será deshabilitado.", exc_info=True)
                self.kafka_enabled = False # Deshabilitar Kafka si falla la inicialización
        else:
            self.logger.info("Kafka deshabilitado por configuración (ENABLE_KAFKA es False).")


    # def process_and_store_file(self, file_stream, user_id, db_session):
    def process_and_store_file(self, file_stream, user_id, db_session, file_metadata=None): # MODIFIED: Add metadata parameter
        """
        Procesa un archivo, lo encripta, lo almacena en MinIO y registra metadatos en la DB.
        
        Args:
            file_stream: Objeto de archivo de Flask (request.files['file']).
            user_id (UUID): El ID del usuario que sube el archivo.
            db_session: Sesión de SQLAlchemy para interactuar con la base de datos.
        
        Returns:
            dict: Información del archivo almacenado.
        
        Raises:
            Exception: Si ocurre un error durante el procesamiento o almacenamiento.
        """
        original_filename = file_stream.filename
        mimetype = file_stream.mimetype
        file_content = file_stream.read()
        file_size = len(file_content)

        self.logger.info(f"Procesando archivo: '{original_filename}' (Tamaño: {file_size} bytes, Tipo: {mimetype}) para usuario: {user_id}")

        # 1. Generar una clave de encriptación única para este archivo
        file_encryption_key = Fernet.generate_key()
        file_fernet = Fernet(file_encryption_key)

        # 2. Encriptar el contenido del archivo
        encrypted_file_content = file_fernet.encrypt(file_content)

        # 3. Encriptar la clave del archivo con la clave maestra del sistema
        encryption_key_encrypted_with_master = self.fernet_master.encrypt(file_encryption_key)

        # 4. Generar un ID único para el archivo en MinIO (UUID para 'ceph_path')
        minio_object_name = str(uuid.uuid4()) 
        
        try:
            # Asegura que el bucket existe
            # bucket_exists() lanza una excepción si hay problemas de conexión/autenticación
            if not self.minio_client.bucket_exists(self.s3_bucket_name):
                self.minio_client.make_bucket(self.s3_bucket_name)
                self.logger.info(f"Bucket '{self.s3_bucket_name}' creado en MinIO.")
            else:
                self.logger.info(f"Bucket '{self.s3_bucket_name}' ya existe.")

            # 5. Subir el archivo encriptado a MinIO
            self.minio_client.put_object(
                self.s3_bucket_name,
                minio_object_name,
                data=io.BytesIO(encrypted_file_content),
                length=len(encrypted_file_content),
                content_type="application/octet-stream" # Siempre es octet-stream porque está encriptado
            )
            self.logger.info(f"Archivo encriptado '{original_filename}' subido a MinIO como '{minio_object_name}'.")

            # 6. Registrar metadatos del archivo en la base de datos
            new_file_entry = EncryptedFile(
                user_id=user_id, # Asigna el user_id
                ceph_path=minio_object_name, # La clave del objeto en MinIO
                encryption_key_encrypted=encryption_key_encrypted_with_master,
                original_filename=original_filename,
                mimetype=mimetype,
                size_bytes=file_size,
                upload_timestamp=datetime.now(),
                processed_status='pending', # Valor por defecto explícito
                last_processed_at=None, # Valor por defecto explícito
                file_metadata=file_metadata # NEW: Assign the passed metadata
                # metadata se puede añadir aquí si se proporciona en el request
            )
            
            db_session.add(new_file_entry)
            db_session.commit() # Guarda el nuevo registro en la base de datos
            self.logger.info(f"Metadatos del archivo '{original_filename}' guardados en la DB con ID: {new_file_entry.id}")

            # 7. Enviar mensaje a Kafka (si está habilitado)
            if self.kafka_enabled and self.kafka_producer:
                message = {
                    "file_id": str(new_file_entry.id),
                    "original_filename": original_filename,
                    "ceph_path": minio_object_name,
                    "user_id": str(user_id),
                    "timestamp": datetime.now().isoformat(),
                    "mimetype": mimetype,
                    "size_bytes": file_size
                }
                try:
                    future = self.kafka_producer.send(self.kafka_topic_uploaded, value=message)
                    record_metadata = future.get(timeout=10) # Espera 10 segundos para la confirmación
                    self.logger.info(f"Mensaje Kafka enviado y confirmado para el archivo ID: {new_file_entry.id} - Topic: {record_metadata.topic}, Partition: {record_metadata.partition}, Offset: {record_metadata.offset}")
                except Exception as kafka_e:
                    self.logger.error(f"Error al enviar mensaje Kafka para el archivo ID {new_file_entry.id}: {kafka_e}", exc_info=True)
            else:
                self.logger.info("Kafka deshabilitado o productor no inicializado, no se envió mensaje.")

            return {
                "id": str(new_file_entry.id),
                "original_filename": original_filename,
                "ceph_path": minio_object_name,
                "size_bytes": file_size,
                "upload_timestamp": new_file_entry.upload_timestamp.isoformat(),
                "status": "upload_complete_and_metadata_saved",
                "processed_status": new_file_entry.processed_status
            }

        except S3Error as e:
            self.logger.error(f"Error de MinIO (S3) durante la subida: {e}", exc_info=True)
            db_session.rollback() # Revierte la transacción si falla MinIO
            raise Exception(f"Error de almacenamiento en MinIO: {e}")
        except Exception as e: # Captura cualquier otra excepción general
            self.logger.error(f"Error inesperado en process_and_store_file: {e}", exc_info=True)
            db_session.rollback() # Revierte la transacción en caso de error
            raise # Re-lanza la excepción

    def retrieve_and_decrypt_file(self, encrypted_file_entry):
        """
        Descarga un archivo encriptado de MinIO, lo desencripta
        y retorna su contenido original.
        
        Args:
            encrypted_file_entry (EncryptedFile): Objeto de modelo EncryptedFile de la DB.
        
        Returns:
            bytes: Contenido original desencriptado del archivo.
            
        Raises:
            Exception: Si ocurre un error durante la recuperación o desencriptación.
        """
        self.logger.info(f"Recuperando archivo encriptado: '{encrypted_file_entry.original_filename}' (MinIO path: {encrypted_file_entry.ceph_path})")
        try:
            # 1. Descargar el archivo encriptado de Minio
            response = self.minio_client.get_object(
                self.s3_bucket_name,
                encrypted_file_entry.ceph_path
            )
            encrypted_content = response.read()
            response.close()
            response.release_conn() # Libera la conexión
            self.logger.info(f"Archivo encriptado '{encrypted_file_entry.ceph_path}' descargado de MinIO.")

            # 2. Desencriptar la clave del archivo con la clave maestra del sistema
            file_encryption_key = self.fernet_master.decrypt(encrypted_file_entry.encryption_key_encrypted)
            file_fernet = Fernet(file_encryption_key)

            # 3. Desencriptar el contenido del archivo
            decrypted_content = file_fernet.decrypt(encrypted_content)
            self.logger.info(f"Archivo '{encrypted_file_entry.original_filename}' desencriptado exitosamente.")

            return decrypted_content

        except S3Error as e:
            self.logger.error(f"Error de MinIO (S3) al descargar o acceder: {e}", exc_info=True)
            raise Exception(f"Error al recuperar archivo de MinIO: {e}")
        except Exception as e:
            self.logger.error(f"Error inesperado al desencriptar o recuperar el archivo: {e}", exc_info=True)
            raise
