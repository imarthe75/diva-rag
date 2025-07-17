import os
import io
import uuid
import logging
from minio import Minio
from minio.error import S3Error
from cryptography.fernet import Fernet
import pyclamd # Para el escaneo de virus
from kafka import KafkaProducer # Para enviar mensajes a Kafka

# No importes EncryptedFile ni User aquí si los estás reemplazando por Document y DocumentVersion
# Los modelos se manejan en app.py, FileProcessorService solo devuelve los datos.

class FileProcessorService:
    def __init__(self, s3_endpoint_url, s3_access_key, s3_secret_key, s3_bucket_name, master_key, kafka_bootstrap_servers=None, kafka_topic_uploaded=None):
        # --- Configuración de Logging ---
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

        # --- Configuración de MinIO ---
        minio_host = s3_endpoint_url.replace("http://", "").replace("https://", "")
        secure_connection = s3_endpoint_url.startswith("https://")
        
        try:
            self.s3_client = Minio(
                minio_host,
                access_key=s3_access_key,
                secret_key=s3_secret_key,
                secure=secure_connection
            )
            self.s3_bucket_name = s3_bucket_name
            self.logger.info(f"Cliente MinIO inicializado para {s3_endpoint_url}.")
        except Exception as e:
            self.logger.error(f"Error al inicializar el cliente MinIO: {e}")
            raise

        # Verificar existencia del bucket
        try:
            if not self.s3_client.bucket_exists(self.s3_bucket_name):
                self.s3_client.make_bucket(self.s3_bucket_name)
                self.logger.info(f"Bucket '{self.s3_bucket_name}' creado en MinIO/Ceph.")
        except S3Error as e:
            self.logger.error(f"Error al verificar/crear bucket en MinIO/Ceph: {e}")
            raise

        # --- Configuración de Fernet (encriptación simétrica) ---
        try:
            self.fernet_master = Fernet(master_key.encode('utf-8'))
            self.logger.info("Clave maestra de Fernet cargada correctamente.")
        except Exception as e:
            self.logger.error(f"Error al cargar la clave maestra de Fernet: {e}. Asegúrate de que SYSTEM_MASTER_KEY sea una clave Fernet válida en Base64.")
            raise

        # --- Configuración de Kafka ---
        self.kafka_producer = None
        self.kafka_topic_uploaded = kafka_topic_uploaded
        self.kafka_enabled = os.getenv("ENABLE_KAFKA", "False").lower() == "true" # Usar variable de entorno para habilitar/deshabilitar

        if self.kafka_enabled and kafka_bootstrap_servers:
            try:
                self.kafka_producer = KafkaProducer(
                    bootstrap_servers=kafka_bootstrap_servers.split(','),
                    value_serializer=lambda v: v.encode('utf-8') # Cambio a encode directo para enviar el ID de la versión
                )
                self.logger.info(f"Kafka Producer inicializado para {kafka_bootstrap_servers}")
            except Exception as e:
                self.logger.error(f"Error al inicializar Kafka Producer: {e}", exc_info=True)
                self.kafka_producer = None
                self.kafka_enabled = False # Deshabilitar Kafka si falla la inicialización
        else:
            self.logger.info("Kafka deshabilitado por configuración o parámetros.")


        # --- Configuración de ClamAV ---
        self.clamav_enabled = os.getenv("CLAMAV_ENABLED", "false").lower() == "true"
        if self.clamav_enabled:
            clamav_host = os.getenv("CLAMAV_HOST", "clamav") # <--- CAMBIADO DE "localhost" A "clamav"
            clamav_port = int(os.getenv("CLAMAV_PORT", "3310"))
            try:
                # Intenta conectarte a ClamAV
                self.clamav_client = pyclamd.ClamdNetworkSocket(clamav_host, clamav_port)
                self.clamav_client.ping() # Verifica la conexión
                self.logger.info(f"Conexión a ClamAV establecida en {clamav_host}:{clamav_port}.")
            except clamd.ConnectionError as e: # <--- CORREGIDO: ERA pyclamd.clamd.ConnectionError
                self.logger.error(f"No se pudo conectar a ClamAV en {clamav_host}:{clamav_port}: {e}")
                self.clamav_enabled = False # Deshabilita la funcionalidad si no se puede conectar
            except Exception as e:
                self.logger.error(f"Error inesperado al inicializar ClamAV: {e}", exc_info=True)
                self.clamav_enabled = False # Deshabilita la funcionalidad

            
    def _generate_file_key(self):
        """Genera una clave de encriptación aleatoria para un archivo."""
        return Fernet.generate_key()

    def _encrypt_data(self, data: bytes, file_key: bytes) -> bytes:
        """Encripta datos usando una clave de archivo."""
        f = Fernet(file_key)
        return f.encrypt(data)

    def _decrypt_data(self, encrypted_data: bytes, file_key: bytes) -> bytes:
        """Desencripta datos usando una clave de archivo."""
        f = Fernet(file_key)
        return f.decrypt(encrypted_data)


    def _scan_for_viruses(self, file_stream: io.BytesIO):
        if not self.clamav_enabled:
            self.logger.info("ClamAV no está habilitado o no se pudo conectar. Omitiendo el escaneo de virus.")
            return

        try:
            self.logger.info("Iniciando escaneo de virus con ClamAV...")
            # Rewind the stream to the beginning for ClamAV
            file_stream.seek(0)
            # Send the file content to ClamAV for scanning
            # The 'stream' method is suitable for in-memory file-like objects
            scan_result = self.clamav_client.stream(file_stream)
            # If a virus is found, scan_result will be a dictionary, e.g., {'stream': ('FOUND', 'EICAR_Test_File')}
            # If no virus is found, scan_result will be None
            file_stream.seek(0) # Reset stream position after scanning
            if scan_result and isinstance(scan_result, dict):
                # Iterate through the stream results to check for 'FOUND' status
                for file_path, scan_status in scan_result.items():
                    if scan_status[0] == 'FOUND':
                        virus_name = scan_status[1]
                        self.logger.warning(f"¡ADVERTENCIA! Virus detectado: {virus_name}")
                        raise ValueError(f"Virus detectado en el archivo: {virus_name}")
            self.logger.info("Escaneo de virus completado. No se detectaron amenazas.")
        except clamd.ConnectionError as e: # <--- CORREGIDO AQUÍ TAMBIÉN
            self.logger.error(f"Error de conexión con ClamAV durante el escaneo: {e}. Deshabilitando ClamAV.")
            self.clamav_enabled = False
            # Decide si quieres re-lanzar la excepción o simplemente continuar sin escaneo
            # Para este caso, solo registramos y el archivo se procesará sin escaneo.
        except Exception as e:
            self.logger.error(f"Error al escanear archivo con ClamAV: {e}", exc_info=True)
            raise ValueError(f"Error en el escaneo de virus: {e}")

    def process_and_store_file(self, file_stream, user_id):
        """
        Procesa un archivo subido: genera clave, encripta, guarda en MinIO.
        NO GUARDA EN DB AQUÍ. Devuelve la información necesaria para crear DocumentVersion.
        """
        original_filename = file_stream.filename
        mimetype = file_stream.mimetype
        file_size = file_stream.content_length
        file_content = file_stream.read() # Leer todo el contenido del archivo en memoria

        self.logger.info(f"Procesando archivo: '{original_filename}' (Tamaño: {file_size} bytes, Tipo: {mimetype}) para usuario: {user_id}")

        # Escanear el archivo en busca de virus
        scan_status = self._scan_for_viruses(file_content)
        if scan_status == "infected":
            self.logger.error(f"Archivo '{original_filename}' infectado, no se almacenará.")
            raise ValueError(f"Virus detectado: {scan_status}. No se pudo procesar el archivo.")
        elif scan_status == "scan_failed":
             self.logger.warning(f"Fallo el escaneo de virus para '{original_filename}', el archivo se almacenará con una advertencia.")
             # Aquí puedes decidir si quieres levantar una excepción o solo advertir.
             # Por ahora, permitimos el almacenamiento pero registramos la advertencia.

        # Generar clave de archivo y encriptar datos
        file_key = self._generate_file_key()
        encrypted_data = self._encrypt_data(file_content, file_key)

        # Encriptar la clave del archivo con la master key del sistema
        encryption_key_encrypted = self.fernet_master.encrypt(file_key)

        # Generar un nombre único para el objeto en MinIO/Ceph
        # Se incluye el user_id para organizar los objetos en MinIO por usuario (opcional pero bueno)
        ceph_path = f"{user_id}/{uuid.uuid4()}-{original_filename}"

        try:
            # Subir el archivo encriptado a MinIO/Ceph
            self.s3_client.put_object(
                self.s3_bucket_name,
                ceph_path,
                io.BytesIO(encrypted_data),
                len(encrypted_data),
                content_type="application/octet-stream" # Siempre como octet-stream porque está encriptado
            )
            self.logger.info(f"Archivo encriptado '{original_filename}' subido a MinIO/Ceph como '{ceph_path}'.")

            # Retornar la información necesaria para el modelo DocumentVersion
            return {
                "ceph_path": ceph_path,
                "encryption_key_encrypted": encryption_key_encrypted.decode('utf-8'), # Decodificar a string para guardar en DB
                "file_size": file_size,
                "mimetype": mimetype,
                "original_filename": original_filename,
                "virus_scan_status": scan_status # Devolver el estado del escaneo de virus
            }
        except S3Error as e:
            self.logger.error(f"Error al subir el archivo a MinIO/Ceph: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Error inesperado en process_and_store_file: {e}", exc_info=True)
            raise

    # La función retrieve_and_decrypt_file debe recibir un objeto DocumentVersion
    def retrieve_and_decrypt_file(self, document_version_entry):
        """
        Recupera un archivo de MinIO, lo desencripta usando la clave encriptada
        y la master key del sistema.
        """
        self.logger.info(f"Recuperando y desencriptando archivo: '{document_version_entry.original_filename}' (MinIO path: {document_version_entry.ceph_path})")
        try:
            # 1. Obtener la clave de encriptación del archivo (encriptada con la master key)
            encryption_key_encrypted = document_version_entry.encryption_key_encrypted
            # Asegurarse de que es bytes para Fernet
            if not isinstance(encryption_key_encrypted, bytes):
                encryption_key_encrypted = encryption_key_encrypted.encode('utf-8')

            # 2. Desencriptar la clave del archivo con la master key del sistema
            file_key = self.fernet_master.decrypt(encryption_key_encrypted)

            # 3. Descargar el archivo encriptado de MinIO/Ceph
            response = self.s3_client.get_object(self.s3_bucket_name, document_version_entry.ceph_path)
            encrypted_data = response.read()
            response.close()
            response.release_conn()
            self.logger.info(f"Archivo '{document_version_entry.ceph_path}' descargado de MinIO/Ceph.")

            # 4. Desencriptar los datos del archivo
            decrypted_data = self._decrypt_data(encrypted_data, file_key)
            self.logger.info(f"Archivo '{document_version_entry.original_filename}' desencriptado exitosamente.")
            return decrypted_data

        except S3Error as e:
            self.logger.error(f"Error S3 al recuperar o desencriptar el archivo: {e}")
            raise ValueError(f"Error al recuperar el archivo de almacenamiento: {e}")
        except Exception as e:
            self.logger.error(f"Error al desencriptar o procesar el archivo: {e}", exc_info=True)
            raise ValueError(f"Error al procesar el archivo: {e}")

    def delete_file_from_minio(self, ceph_path: str):
        """Elimina un archivo del bucket de MinIO/Ceph."""
        self.logger.info(f"Eliminando archivo: '{ceph_path}' de MinIO/Ceph.")
        try:
            self.s3_client.remove_object(self.s3_bucket_name, ceph_path)
            self.logger.info(f"Archivo '{ceph_path}' eliminado de MinIO/Ceph.")
        except S3Error as e:
            self.logger.error(f"Error S3 al eliminar el archivo '{ceph_path}' de MinIO/Ceph: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Error inesperado al eliminar el archivo '{ceph_path}' de MinIO/Ceph: {e}", exc_info=True)
            raise