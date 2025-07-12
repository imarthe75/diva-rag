# Digital Vault Project: RAG Analyser 🚀

![Digital Vault Logo](./docs/logo.png)

 ## 🎯 Objetivo del Proyecto

El **Digital Vault Project** es un sistema de gestión de documentos seguro e inteligente, diseñado para permitir a los usuarios subir, cifrar, almacenar y consultar sus documentos de forma eficiente y confidencial. Utiliza tecnologías modernas de contenedores (Docker), bases de datos vectoriales (PgVector), almacenamiento de objetos (MinIO) y capacidades avanzadas de Procesamiento de Lenguaje Natural (NLP) impulsadas por modelos de lenguaje grandes (LLM) a través de Ollama.

Este proyecto implementa una bóveda digital segura con capacidades de análisis RAG (Retrieval-Augmented Generation) para responder a preguntas sobre el contenido de los documentos almacenados. Utiliza un stack moderno con Flask, Celery, PostgreSQL (con PgVector), MinIO, Kafka, y Ollama para procesamiento de lenguaje natural.

El objetivo principal es proporcionar una plataforma robusta para:
1.  **Almacenamiento Seguro:** Cifrado de documentos y almacenamiento en MinIO.
2.  **Búsqueda Semántica Avanzada (RAG):** Indexación de contenido de documentos para permitir consultas de lenguaje natural mediante Retrieval Augmented Generation (RAG).
3.  **Interacción Inteligente:** Permite a los usuarios hacer preguntas sobre el contenido de sus documentos indexados, obteniendo respuestas concisas y basadas en la información disponible.
4.  **Escalabilidad y Flexibilidad:** Arquitectura basada en microservicios y Docker Compose para facilitar el despliegue y la escalabilidad.

## 📝 Descripción General del Proyecto

El proyecto está compuesto por varios servicios orquestados con Docker Compose:

* **`flask_backend` (Python/Flask):** La API principal que maneja la autenticación de usuarios, la gestión de archivos (subida, cifrado/descifrado, descarga), y la interacción con los modelos de lenguaje para RAG.
* **`celery_worker` (Python/Celery):** Un worker asíncrono que procesa las tareas pesadas en segundo plano, como la extracción de texto, la generación de embeddings y la indexación en la base de datos vectorial.
* **`ollama`:** El servidor de modelos de lenguaje grandes (LLM) que proporciona capacidades de embedding y generación de texto. Permite el uso de modelos como `nomic-embed-text` para embeddings y `phi3` o `llama3` para generación.
* **`postgres_db` (PostgreSQL con PgVector):** La base de datos relacional principal que almacena metadatos de usuarios y archivos, así como los chunks de texto y sus embeddings vectoriales.
* **`minio`:** Un servidor de almacenamiento de objetos compatible con S3, utilizado para guardar los archivos cifrados de los usuarios.
* **`valkey` (Redis-compatible):** Utilizado como broker y backend de resultados para Celery.
* **`kafka` / `zookeeper`:** Componentes de un sistema de mensajería asíncrono, aunque su uso actual en los logs es para depuración/logging de eventos, puede extenderse para eventos más complejos en el futuro.
* **Hashicorp Vault (Planificado):** Para una gestión segura y centralizada de secretos.
* **Firma Digital (Planificado):** Como método avanzado de autenticación.


## 🚀 Cómo Funciona

Cuando un usuario sube un archivo:
1.  El `flask_backend` recibe el archivo, lo cifra y lo guarda en MinIO.
2.  Se crea una tarea en Celery para procesar el archivo.
3.  El `celery_worker` descarga el archivo cifrado de MinIO, lo descifra y extrae el texto (ej. de PDFs).
4.  El texto se divide en "chunks" (fragmentos).
5.  Cada chunk se envía al servidor `ollama` para generar un **embedding** (una representación numérica vectorial del texto) usando el modelo `nomic-embed-text`.
6.  Los chunks y sus embeddings se almacenan en la tabla `document_chunks` en `postgres_db` (utilizando la extensión PgVector).

Cuando un usuario realiza una pregunta (consulta RAG):
1.  La pregunta del usuario se envía al `flask_backend`.
2.  El `flask_backend` utiliza `ollama` para generar un **embedding** de la pregunta del usuario.
3.  Este embedding se utiliza para realizar una búsqueda de similitud vectorial en `postgres_db` para encontrar los chunks de documentos más relevantes.
4.  Los chunks recuperados (`retrieved_chunks`) se combinan con la pregunta del usuario para formar un nuevo **prompt de contexto**.
5.  Este prompt completo se envía al `ollama` (al modelo de generación como `phi3` o `llama3`).
6.  El modelo de generación utiliza el contexto para responder la pregunta del usuario.

## 🔗 Endpoints Principales

Aquí se describen los endpoints más relevantes de la API `flask_backend`:

### **Autenticación y Usuarios**

* **`POST /register`**
    * **Descripción:** Registra un nuevo usuario en el sistema.
    * **Request Body:**
        ```json
        {
          "username": "nuevo_usuario",
          "password": "mi_password_segura",
          "email": "usuario@ejemplo.com"
        }
        ```
    * **Response:**
        ```json
        {
          "message": "User registered successfully",
          "user_id": "uuid-del-usuario-registrado"
        }
        ```

* **`POST /login`**
    * **Descripción:** Autentica a un usuario y devuelve un token JWT.
    * **Request Body:**
        ```json
        {
          "username": "usuario_existente",
          "password": "su_password"
        }
        ```
    * **Response (Success):**
        ```json
        {
          "access_token": "eyJhbGciOiJIUzI1Ni...",
          "message": "Login successful"
        }
        ```
    * **Response (Failure):**
        ```json
        {
          "message": "Invalid credentials"
        }
        ```

### **Gestión de Archivos (Requiere JWT)**

* **`POST /upload`**
    * **Descripción:** Sube y cifra un nuevo documento. El procesamiento RAG se inicia de forma asíncrona.
    * **Headers:** `Authorization: Bearer <your_jwt_token>`
    * **Request Body:** `multipart/form-data` con un campo `file` que contiene el documento.
    * **Response:**
        ```json
        {
          "message": "File uploaded and queued for processing",
          "file_id": "uuid-del-archivo",
          "filename": "nombre_original_del_archivo.pdf"
        }
        ```

* **`GET /files`**
    * **Descripción:** Lista todos los archivos subidos por el usuario actual.
    * **Headers:** `Authorization: Bearer <your_jwt_token>`
    * **Response:**
        ```json
        [
          {
            "id": "uuid-archivo-1",
            "original_filename": "doc1.pdf",
            "uploaded_at": "2025-07-10T10:00:00Z",
            "processed_status": "indexed"
          },
          {
            "id": "uuid-archivo-2",
            "original_filename": "reporte.docx",
            "uploaded_at": "2025-07-10T11:30:00Z",
            "processed_status": "pending"
          }
        ]
        ```

* **`GET /download/<file_id>`**
    * **Descripción:** Descarga un archivo específico del usuario, descifrándolo al vuelo.
    * **Headers:** `Authorization: Bearer <your_jwt_token>`
    * **Parámetros de Ruta:** `file_id` (UUID del archivo a descargar).
    * **Response:** El archivo binario descifrado.

* **`DELETE /files/<file_id>`**
    * **Descripción:** Elimina un archivo específico del usuario de MinIO y de la base de datos (incluyendo sus chunks y embeddings).
    * **Headers:** `Authorization: Bearer <your_jwt_token>`
    * **Parámetros de Ruta:** `file_id` (UUID del archivo a eliminar).
    * **Response:**
        ```json
        {
          "message": "File deleted successfully"
        }
        ```

### **Consulta RAG (Requiere JWT)**

* **`POST /query`**
    * **Descripción:** Permite a los usuarios hacer preguntas sobre el contenido de sus documentos indexados.
    * **Headers:** `Authorization: Bearer <your_jwt_token>`
    * **Request Body:**
        ```json
        {
          "question": "¿Cuál es el resumen del informe anual de 2023?"
        }
        ```
    * **Response:**
        ```json
        {
          "answer": "Según el informe anual de 2023, los principales hallazgos son..."
        }
        ```
        *Nota: Este endpoint es donde podrías experimentar timeouts si el modelo de generación es muy lento para tu hardware, como se observó en los logs.*

---

**Nota Importante sobre los Timeouts:**
Como se observó en los logs recientes, el endpoint `/query` que interactúa con el modelo de generación de Ollama (`phi3:3.8b-mini-4k-instruct-q4_K_M`) puede experimentar `WORKER TIMEOUT` si el modelo toma más tiempo del configurado (actualmente 3 minutos). Para mejorar esto, se ha recomendado:
1.  **Aumentar el timeout de Gunicorn** para el servicio `flask_backend` en `docker-compose.yml` a un valor mayor (ej. 300 segundos o más).
2.  **Considerar modelos más ligeros** o buscar optimizaciones adicionales si los timeouts persisten.


## 🛠️ Instalación y Configuración
Prerrequisitos
Docker y Docker Compose.

make (opcional, para comandos de conveniencia).

Pasos
Clonar el Repositorio:

Bash

git clone https://github.com/tu_usuario/tu_repositorio.git
cd tu_repositorio

Configurar Variables de Entorno:
Crea un archivo .env en la raíz del proyecto (al lado de docker-compose.yml) con las siguientes variables:

Fragmento de código

```
# Variables de PostgreSQL
POSTGRES_DB=digital_vault_db
POSTGRES_USER=dvu
POSTGRES_PASSWORD=secret

# Variables de MinIO
MINIO_ROOT_USER=minio_admin
MINIO_ROOT_PASSWORD=minio_secret_password
MINIO_BUCKET_NAME=document-vault

# Clave Secreta para JWT (JSON Web Tokens) - ¡CAMBIA ESTA CLAVE EN PRODUCCIÓN!
JWT_SECRET_KEY=supersecretjwtkey

# Clave de cifrado para documentos (Fernet). Genera una nueva:
# from cryptography.fernet import Fernet
# Fernet.generate_key().decode()
DOCUMENT_ENCRYPTION_KEY=tu_clave_de_cifrado_fernet

# Configuración de Ollama (modelos)
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
OLLAMA_GENERATION_MODEL=phi3:3.8b-mini-4k-instruct-q4_K_M
```

Nota de Seguridad: Para un entorno de producción, considera usar Hashicorp Vault para gestionar JWT_SECRET_KEY y DOCUMENT_ENCRYPTION_KEY de forma segura.

Iniciar los Servicios Docker:

Bash
```
docker compose up --build -d
```

El comando --build es crucial la primera vez o después de modificar los Dockerfiles, ya que instalará Calibre y Tesseract OCR dentro del contenedor del Celery Worker.

Verificar Servicios:

Bash
```
docker compose ps
```

Todos los servicios (postgres_db, minio, valkey, kafka, zookeeper, ollama, flask_backend, celery_worker) deberían estar en estado running o healthy.

## 📄 Formatos de Documentos Soportados
El sistema puede extraer texto y procesar los siguientes tipos de archivos, preparando su contenido para el análisis RAG:

* .pdf (usando pypdf)
* .txt (texto plano)
* .mobi (usando la librería mobi de Python)
* .docx (Microsoft Word, usando python-docx)
* .xlsx (Microsoft Excel, usando openpyxl. Extrae contenido de celdas)
* .pptx (Microsoft PowerPoint, usando python-pptx. Extrae texto de diapositivas)
* .epub (EPUB e-books, usando Ebooklib y html2text)
* .azw3 (Kindle Format 8, usando Calibre/ebook-convert para una extracción robusta)

* Imágenes con texto (.png, .jpg, .jpeg, .gif, .bmp, .tiff) a través de OCR (Tesseract OCR).

## ⏱️ Configuración de Zona Horaria en Logs
Por defecto, los contenedores Docker registran las horas en UTC. Para alinear las horas de los logs con tu zona horaria local (ej. America/Mexico_City), añade la siguiente variable de entorno a cada servicio relevante en tu docker-compose.yml:

* **YAML**
```
  # ... en cada servicio relevante (ej. flask_backend, celery_worker, postgres_db, etc.)
  environment:
  # ... otras variables ...
  TZ: America/Mexico_City # O tu zona horaria específica, ej. America/Monterrey
```

Después de modificar docker-compose.yml, ejecuta docker compose down && docker compose up -d para aplicar los cambios.

## 👩‍💻 Flujo de Desarrollo Recomendado
Para mantener tu entorno de desarrollo (VS Code en tu laptop) y tu entorno en la nube (instancia linux) sincronizados, utiliza GitHub como tu "fuente de verdad" central.

* Desde tu Laptop (VS Code):
* Clona o actualiza tu repositorio localmente (git pull origin main).
* Realiza tus cambios de código.
* Guarda, prepara (stage) y confirma (commit) tus cambios.
* Empuja (push) tus cambios a GitHub (git push origin main).
* Desde tu Instancia (con VS Code Remote - SSH):
* Conéctate a tu servidor via SSH desde VS Code y abre la carpeta del proyecto.

Abre la terminal integrada de VS Code (que estará en tu servidor).

Descarga los últimos cambios de GitHub (git pull origin main).

Si tus cambios afectan los Dockerfiles o el código de los servicios, reconstruye y reinicia los contenedores para aplicar los cambios:

Bash
```
docker compose down
docker compose up --build -d
```

## 🛣️ Próximos Pasos (Planificados)
Implementación de Hashicorp Vault: Integrar Vault para la gestión segura y dinámica de secretos (claves de cifrado, credenciales de DB, etc.).

Autenticación con Firma Digital: Explorar y añadir un método de autenticación basado en firma digital para mayor seguridad y flexibilidad.

Optimización Asíncrona: Refinar y optimizar las operaciones asíncronas de Celery para mayor rendimiento y escalabilidad.

## 🤝 Contribución
¡Las contribuciones son bienvenidas! Por favor, abre un "issue" o "pull request" en el repositorio de GitHub.