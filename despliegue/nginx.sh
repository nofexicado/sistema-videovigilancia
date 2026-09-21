#!/usr/bin/env bash
# Pone nginx con TLS delante de Sistema de Videovigilancia. Se corre EN EL SERVIDOR, con sudo,
# despues de instalar.sh.
#
#   sudo bash /opt/sistema-videovigilancia/despliegue/nginx.sh
#
# Certificado autofirmado por IP (red interna, sin dominio publico). El
# navegador va a avisar la primera vez en cada equipo: hay que aceptar la
# excepcion una sola vez. Es idempotente.
set -euo pipefail

IP=${SISTEMA_VIDEOVIGILANCIA_IP:?"definí SISTEMA_VIDEOVIGILANCIA_IP con la IP del servidor"}
CERTDIR=/etc/ssl/sistema-videovigilancia
RAIZ=/opt/sistema-videovigilancia

echo "==> nginx y openssl"
apt-get install -y -qq nginx openssl

echo "==> certificado autofirmado para $IP"
mkdir -p "$CERTDIR"
if [ -f "$CERTDIR/sistema-videovigilancia.crt" ]; then
  echo "    ya existe, no se regenera (borralo a mano para rehacerlo)"
else
  # subjectAltName con la IP: sin esto los navegadores modernos rechazan el
  # certificado aunque el CN coincida.
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$CERTDIR/sistema-videovigilancia.key" -out "$CERTDIR/sistema-videovigilancia.crt" \
    -subj "/CN=$IP/O=Sistema de Videovigilancia" \
    -addext "subjectAltName=IP:$IP"
  chmod 600 "$CERTDIR/sistema-videovigilancia.key"
  echo "    generado (válido 10 años)"
fi

echo "==> acceso de nginx a las grabaciones"
# nginx sirve /media/ directamente --ver el comentario en sistema-videovigilancia.nginx-- asi
# que www-data tiene que poder leer el arbol de video. Todo lo que hay debajo
# de SISTEMA_VIDEOVIGILANCIA_DATOS ya nace legible; el unico cerrojo es el directorio raiz,
# que es 750 de sistema-videovigilancia:sistema-videovigilancia. Sumar www-data al grupo alcanza, y es menos
# que abrirlo a todo el mundo con un chmod.
if getent group sistema-videovigilancia >/dev/null 2>&1; then
  usermod -aG sistema-videovigilancia www-data
  echo "    www-data agregado al grupo sistema-videovigilancia"
fi
# El cache de las autorizaciones de video (proxy_cache_path en sistema-videovigilancia.nginx).
install -d -o www-data -g www-data /var/cache/nginx/sistema-videovigilancia-sesion

echo "==> sitio nginx"
install -m 644 "$RAIZ/despliegue/sistema-videovigilancia.nginx" /etc/nginx/sites-available/sistema-videovigilancia
ln -sf /etc/nginx/sites-available/sistema-videovigilancia /etc/nginx/sites-enabled/sistema-videovigilancia
# El sitio por defecto responde en 80 y estorba; se saca de habilitados.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "==> firewall"
# Solo si hay ufw activo: abrir 80/443, cerrar el 8000 directo (ahora la app
# vive detras de nginx y no debe alcanzarse sin pasar por TLS).
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp  >/dev/null || true
  ufw allow 443/tcp >/dev/null || true
  ufw delete allow 8000/tcp >/dev/null 2>&1 || true
  echo "    ufw: 80 y 443 abiertos, 8000 cerrado"
else
  echo "    ufw no está activo; si usás otro firewall, abrí 80/443 y cerrá 8000"
fi

echo
echo "==> comprobación"
sleep 1
COD=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/salud || echo 000)
if [ "$COD" = "200" ]; then
  echo "    HTTPS responde: https://$IP"
  echo "    (el navegador pedirá aceptar el certificado la primera vez)"
else
  echo "    HTTPS NO responde (código $COD) — revisá: nginx -t; journalctl -u nginx -n 30"
  exit 1
fi
