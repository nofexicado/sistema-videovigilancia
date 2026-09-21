# Sube Sistema de Videovigilancia al servidor desde la notebook. Se corre EN WINDOWS.
#
#   .\despliegue\subir.ps1 -Usuario tuusuario -Servidor 192.0.2.10
#   .\despliegue\subir.ps1 -Usuario tuusuario -Servidor 192.0.2.10 -ConBase   # además la base y la llave
#
# Arma un .tar.gz con el código y lo copia por scp. NO sube las grabaciones de
# prueba (datos/video/, 3,5 GB) ni tools/credentials.json, que tiene claves en
# texto plano y no debe salir de la notebook.
param(
  [Parameter(Mandatory = $true)][string]$Usuario,
  [Parameter(Mandatory = $true)][string]$Servidor,
  [switch]$ConBase
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

Write-Host '==> armando el paquete'
# El nombre de salida va RELATIVO a propósito: si en el PATH está el tar de
# MSYS/Git, una ruta "C:\..." la interpreta como host remoto y falla con
# "Cannot connect to C: resolve failed".
$paquete = Join-Path $raiz 'sistema-videovigilancia.tar.gz'
if (Test-Path $paquete) { Remove-Item $paquete }
# Sólo lo que el servidor necesita para correr.
tar -czf sistema-videovigilancia.tar.gz `
  --exclude='*/__pycache__' --exclude='__pycache__' `
  --exclude='tools/credentials.json' `
  backend web inventory tools despliegue README.md
if (-not (Test-Path $paquete)) { throw 'no se pudo armar el paquete' }
$mb = [math]::Round((Get-Item $paquete).Length / 1MB, 1)
Write-Host "    sistema-videovigilancia.tar.gz  $mb MB"

Write-Host '==> copiando al servidor'
scp $paquete "${Usuario}@${Servidor}:/tmp/sistema-videovigilancia.tar.gz"

if ($ConBase) {
  Write-Host '==> copiando la base y la llave'
  Write-Host '    (corré antes: py backend\gestionar.py mudanza --si)'
  scp datos\sistema-videovigilancia.db "${Usuario}@${Servidor}:/tmp/sistema-videovigilancia.db"
  scp datos\clave.key  "${Usuario}@${Servidor}:/tmp/clave.key"
}

Write-Host '==> desempaquetando'
ssh "${Usuario}@${Servidor}" @'
set -e
sudo mkdir -p /opt/sistema-videovigilancia
sudo tar -xzf /tmp/sistema-videovigilancia.tar.gz -C /opt/sistema-videovigilancia
rm -f /tmp/sistema-videovigilancia.tar.gz
echo "código en /opt/sistema-videovigilancia"
'@

if ($ConBase) {
  ssh "${Usuario}@${Servidor}" @'
set -e
sudo mkdir -p /srv/sistema-videovigilancia
sudo mv /tmp/sistema-videovigilancia.db /srv/sistema-videovigilancia/sistema-videovigilancia.db
sudo mv /tmp/clave.key  /srv/sistema-videovigilancia/clave.key
sudo chown -R sistema-videovigilancia:sistema-videovigilancia /srv/sistema-videovigilancia 2>/dev/null || true
sudo chmod 600 /srv/sistema-videovigilancia/clave.key
echo "base y llave en /srv/sistema-videovigilancia"
'@
}

Remove-Item $paquete -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Subido. Ahora, en el servidor:'
Write-Host "  ssh ${Usuario}@${Servidor}"
Write-Host '  sudo bash /opt/sistema-videovigilancia/despliegue/instalar.sh'
