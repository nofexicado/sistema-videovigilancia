#!/usr/bin/env bash
# Instala Sistema de Videovigilancia en el R710. Se corre EN EL SERVIDOR, con sudo, despues de
# haber subido el código a /opt/sistema-videovigilancia.
#
#   sudo bash /opt/sistema-videovigilancia/despliegue/instalar.sh
#
# Es idempotente: se puede volver a correr para actualizar sin romper nada.
# No toca la base ni las grabaciones.
set -euo pipefail

RAIZ=/opt/sistema-videovigilancia
DATOS=${SISTEMA_VIDEOVIGILANCIA_DATOS:-/srv/sistema-videovigilancia}
USUARIO=sistema-videovigilancia

echo "==> paquetes"
apt-get update -qq
# ffprobe viene en ffmpeg y es lo unico que necesita la sonda.
apt-get install -y -qq python3 python3-venv ffmpeg

echo "==> usuario de servicio"
# Sin shell y sin home: este usuario solo corre el servicio.
id -u "$USUARIO" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$USUARIO"

echo "==> directorios"
mkdir -p "$DATOS"
chown "$USUARIO:$USUARIO" "$DATOS"
# La base y la llave son legibles solo por el servicio.
chmod 750 "$DATOS"

echo "==> entorno de Python"
[ -d "$RAIZ/venv" ] || python3 -m venv "$RAIZ/venv"
# El R710 llega a los repos de Ubuntu pero NO a PyPI (red industrial aislada).
# Que pip no pueda salir no es motivo para abortar: las dependencias ya estan
# en el venv de la instalacion anterior, y este script corre sobre todo para
# ACTUALIZAR el codigo. Con `set -e` y un pip que falla, el script se moria
# antes del `systemctl restart` y la actualizacion no llegaba a aplicarse
# nunca. Se aborta solo si las dependencias de verdad faltan.
if "$RAIZ/venv/bin/pip" install --quiet --disable-pip-version-check      -r "$RAIZ/backend/requirements.txt" 2>/dev/null; then
  echo "    dependencias al dia"
else
  echo "    sin acceso a PyPI: se usan las que ya estan en el venv"
  if ! "$RAIZ/venv/bin/python" -c "import fastapi, uvicorn, cryptography" 2>/dev/null; then
    echo "    FALTAN dependencias y no hay desde donde bajarlas" >&2
    exit 1
  fi
fi

echo "==> base de datos"
if [ -f "$DATOS/sistema-videovigilancia.db" ]; then
  echo "    ya existe $DATOS/sistema-videovigilancia.db, no se toca"
else
  echo "    creando base nueva"
  cd "$RAIZ/backend"
  SISTEMA_VIDEOVIGILANCIA_DATOS="$DATOS" "$RAIZ/venv/bin/python" gestionar.py init
  SISTEMA_VIDEOVIGILANCIA_DATOS="$DATOS" "$RAIZ/venv/bin/python" gestionar.py importar
fi
# Solo lo que este script pudo haber creado como root. NO se hace `chown -R`
# sobre $DATOS: ahi vive el archivo de grabaciones —decenas de miles de
# archivos— y ademas el grabador borra segmentos de la ventana en vivo
# mientras chown recorre el arbol, asi que falla con "No such file or
# directory" y, con `set -e`, la instalacion aborta a la mitad. Lo que escribe
# el servicio ya nace con su duenio.
# El `|| true` no es adorno: el estado de salida del `for` es el de su ultima
# vuelta, asi que si el ultimo archivo de la lista no existe el bucle termina
# en 1 y `set -e` mata el script.
for f in sistema-videovigilancia.db sistema-videovigilancia.db-wal sistema-videovigilancia.db-shm clave.key sesion.key; do
  [ -e "$DATOS/$f" ] && chown "$USUARIO:$USUARIO" "$DATOS/$f" || true
done
# La llave de cifrado de las credenciales: solo el duenio.
[ -f "$DATOS/clave.key" ] && chmod 600 "$DATOS/clave.key" || true

echo "==> permisos del código"
# El servicio no necesita escribir en el código, solo leerlo.
chown -R root:root "$RAIZ"
chmod -R go-w "$RAIZ"

echo "==> servicio"
install -m 644 "$RAIZ/despliegue/sistema-videovigilancia.service" /etc/systemd/system/sistema-videovigilancia.service
systemctl daemon-reload
systemctl enable sistema-videovigilancia
systemctl restart sistema-videovigilancia
sleep 3
systemctl --no-pager --lines=15 status sistema-videovigilancia || true

echo
echo "==> comprobación"
if curl -fsS http://127.0.0.1:8000/api/salud >/dev/null; then
  echo "    la API responde en http://$(hostname -I | awk '{print $1}'):8000"
else
  echo "    LA API NO RESPONDE — mirá: journalctl -u sistema-videovigilancia -n 50"
  exit 1
fi

cat <<'FIN'

Listo. Lo que sigue, a mano:

  1. Ver qué cámaras contesta el servidor (no abre sesiones RTSP):
       cd /opt/sistema-videovigilancia && sudo -u sistema-videovigilancia SISTEMA_VIDEOVIGILANCIA_DATOS=/srv/sistema-videovigilancia \
         ./venv/bin/python tools/vivas.py --actualizar

  2. Si la base es nueva, cargar las credenciales de las cámaras:
       cd /opt/sistema-videovigilancia/backend
       sudo -u sistema-videovigilancia SISTEMA_VIDEOVIGILANCIA_DATOS=/srv/sistema-videovigilancia \
         ../venv/bin/python gestionar.py credencial bosch admincrd

  3. Abrir el muro y apretar "Iniciar transmisión". No arranca sola.

ACCESO: la app pide login por rol (operador mira, admin modifica) y uvicorn
escucha sólo en 127.0.0.1 detrás de nginx, que termina TLS en el 443.

OJO con un caso: si la tabla `usuarios` está VACÍA el sistema queda ABIERTO a
propósito, para no autobloquear una instalación nueva. Comprobalo así — tiene
que dar 401:

    curl -sk -o /dev/null -w '%{http_code}
' https://TU-IP/api/grabador

Si da 200, no hay usuarios cargados. Se crean con:

    cd /opt/sistema-videovigilancia/backend
    sudo -u sistema-videovigilancia SISTEMA_VIDEOVIGILANCIA_DATOS=/srv/sistema-videovigilancia       ../venv/bin/python gestionar.py usuario Admin admin
FIN
