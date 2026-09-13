# syntax=docker/dockerfile:1
# ------------------------------------------------------------------------------
# Dédalo - Orquestador visual de flujos de trabajo ultraligero
# Multi-stage build optimizado para bajo consumo de memoria y máxima seguridad
# ------------------------------------------------------------------------------

# ==============================================================================
# Etapa 1: Builder (Instalación de dependencias en venv aislado)
# ==============================================================================
FROM python:3.12-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY dedalo/ ./dedalo/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ==============================================================================
# Etapa 2: Runner (Imagen final mínima y segura sin herramientas de compilación)
# ==============================================================================
FROM python:3.12-slim-bookworm AS runner

# Instalar binarios necesarios del sistema (git para nodos de control de versiones)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

# Argumentos de usuario (por defecto UID/GID 1000, estándar en Raspberry Pi / Ubuntu)
ARG UID=1000
ARG GID=1000

# Buenas prácticas de seguridad: Usuario no privilegiado del sistema
RUN groupadd -g "${GID}" -r dedalo && \
    useradd -u "${UID}" -r -g dedalo -d /app -s /usr/sbin/nologin -c "Dedalo Service User" dedalo

WORKDIR /app

# Copiar el entorno virtual optimizado desde el builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Variables de entorno de rendimiento y memoria
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=1 \
    MALLOC_TRIM_THRESHOLD_=65536 \
    DEDALO_HOST=0.0.0.0 \
    DEDALO_PORT=8080

# Copiar código fuente asignando la propiedad al usuario no privilegiado
COPY --chown=dedalo:dedalo dedalo /app/dedalo

# Preparar directorio de datos con permisos estrictos
RUN mkdir -p /app/data && chown -R dedalo:dedalo /app

# Cambiar al usuario no privilegiado
USER dedalo

# Exponer el puerto del servicio
EXPOSE 8080

# Declarar volumen de datos
VOLUME ["/app/data"]

# Comprobación de salud nativa en Python (sin dependencias extras ni vulnerabilidades de curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

# Manejo limpio de señales de apagado
STOPSIGNAL SIGTERM

# Comando de arranque por defecto (servidor web optimizado)
CMD ["uvicorn", "dedalo.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--limit-concurrency", "15", "--timeout-keep-alive", "5"]
