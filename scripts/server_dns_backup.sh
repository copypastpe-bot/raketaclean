#!/usr/bin/env bash
# Резервные серверы имён (DNS) для VPS 91.200.150.68.
#
# Служба имён на сервере — systemd-resolved, серверы приходят от провайдера по DHCP
# (85.193.93.193 и .194). 15.09.2026 они молчали 44 минуты, и оба бота писали
# «нет связи с amoCRM» и «календарь недоступен», хотя сеть работала. Этот скрипт
# добавляет отдельным файлом netplan два резервных сервера — Яндекс 77.88.8.8 и
# Cloudflare 1.1.1.1. Провайдерские остаются в списке, cloud-init новый файл не трогает.
#
# Запуск (нужен sudo, с локальной машины в своём терминале):
#   scp scripts/server_dns_backup.sh admin@91.200.150.68:/tmp/
#   ssh -t admin@91.200.150.68 'sudo bash /tmp/server_dns_backup.sh'
# Только посмотреть, ничего не меняя (sudo не нужен):
#   bash /tmp/server_dns_backup.sh --check
# Откат: sudo rm /etc/netplan/60-dns-backup.yaml && sudo netplan apply
set -euo pipefail

FILE=/etc/netplan/60-dns-backup.yaml
SERVERS="77.88.8.8, 1.1.1.1"
HOSTS="raketacleancrm.amocrm.ru www.googleapis.com api.telegram.org"

show() {
    echo "--- серверы имён на eth0:"
    resolvectl status eth0 | grep -E 'DNS Servers|Current DNS'
    echo "--- проверка разрешения имён:"
    for h in $HOSTS; do
        if resolvectl query "$h" >/dev/null 2>&1; then echo "  $h: ok"; else echo "  $h: НЕ РАЗРЕШАЕТСЯ"; fi
    done
}

if [ "${1:-}" = "--check" ]; then
    if [ -f "$FILE" ]; then echo "--- $FILE:"; cat "$FILE"; else echo "$FILE нет: резервных серверов не настроено"; fi
    show
    exit 0
fi

if [ "$(id -u)" != 0 ]; then
    echo "Нужен root: sudo bash $0" >&2
    exit 2
fi

if [ -f "$FILE" ]; then
    echo "$FILE уже есть, перезаписываю:"
    cat "$FILE"
fi

cat > "$FILE" <<YAML
# Резервные серверы имён. Провайдерские приходят по DHCP и остаются в списке.
# Поставлено скриптом scripts/server_dns_backup.sh из репозитория raketaclean.
network:
  version: 2
  ethernets:
    eth0:
      nameservers:
        addresses: [$SERVERS]
YAML
chmod 600 "$FILE"
netplan generate

if [ -t 0 ]; then
    echo
    echo "Применяю через netplan try: если связь оборвётся, через 120 секунд всё откатится само."
    echo "Когда попросит — нажмите Enter, чтобы оставить настройку."
    netplan try --timeout 120
else
    echo "Нет терминала, применяю netplan apply."
    netplan apply
fi

sleep 2
show
