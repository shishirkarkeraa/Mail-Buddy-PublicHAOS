#!/bin/sh
set -eu

lan_subnet="${1:-${MAIL_BUDDY_LAN_SUBNET:-}}"
lan_interface="${2:-${MAIL_BUDDY_LAN_INTERFACE:-eth0}}"
lan_ipv6_subnet="${MAIL_BUDDY_LAN_IPV6_SUBNET:-}"

if [ -z "$lan_subnet" ]; then
  echo "Usage: sudo $0 <LAN-CIDR> [LAN-interface]" >&2
  echo "Example: sudo $0 192.168.1.0/24 eth0" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script with sudo; it updates Docker's firewall chain." >&2
  exit 2
fi
if ! command -v iptables >/dev/null 2>&1; then
  echo "iptables is required." >&2
  exit 2
fi
if ! command -v ip >/dev/null 2>&1; then
  echo "The ip command is required." >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to validate the configured network." >&2
  exit 2
fi
if ! ip link show dev "$lan_interface" >/dev/null 2>&1; then
  echo "LAN interface '$lan_interface' does not exist." >&2
  exit 2
fi
if ! python3 -c \
  'import ipaddress,sys; ipaddress.IPv4Network(sys.argv[1], strict=True)' \
  "$lan_subnet" >/dev/null 2>&1; then
  echo "LAN subnet '$lan_subnet' is not a canonical IPv4 CIDR." >&2
  exit 2
fi
if [ -n "$lan_ipv6_subnet" ] && ! python3 -c \
  'import ipaddress,sys; ipaddress.IPv6Network(sys.argv[1], strict=True)' \
  "$lan_ipv6_subnet" >/dev/null 2>&1; then
  echo "LAN IPv6 subnet '$lan_ipv6_subnet' is not a canonical IPv6 CIDR." >&2
  exit 2
fi

iptables_cmd() {
  iptables -w 10 "$@"
}

if ! iptables_cmd -n -L DOCKER-USER >/dev/null 2>&1; then
  echo "Docker's DOCKER-USER chain is unavailable; start Docker first." >&2
  exit 1
fi

# A guard chain keeps TCP/443 fail-closed while an existing policy is rebuilt.
# If this script is interrupted, the guard remains in place instead of exposing
# the published port.
iptables_cmd -N MAIL_BUDDY_GUARD 2>/dev/null || true
iptables_cmd -F MAIL_BUDDY_GUARD
iptables_cmd -A MAIL_BUDDY_GUARD -p tcp --dport 443 -j DROP
iptables_cmd -A MAIL_BUDDY_GUARD -j RETURN
iptables_cmd -C DOCKER-USER -i "$lan_interface" -j MAIL_BUDDY_GUARD \
  2>/dev/null \
  || iptables_cmd -I DOCKER-USER 1 -i "$lan_interface" -j MAIL_BUDDY_GUARD

iptables_cmd -N MAIL_BUDDY_LAN 2>/dev/null || true
iptables_cmd -F MAIL_BUDDY_LAN
iptables_cmd -A MAIL_BUDDY_LAN -p tcp --dport 443 -j DROP
iptables_cmd -I MAIL_BUDDY_LAN 1 \
  -s "$lan_subnet" -p tcp --dport 443 -j RETURN
iptables_cmd -I MAIL_BUDDY_LAN 1 \
  -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
iptables_cmd -A MAIL_BUDDY_LAN -j RETURN

iptables_cmd -C DOCKER-USER -i "$lan_interface" -j MAIL_BUDDY_LAN \
  2>/dev/null \
  || iptables_cmd -I DOCKER-USER 1 -i "$lan_interface" -j MAIL_BUDDY_LAN
iptables_cmd -D DOCKER-USER -i "$lan_interface" -j MAIL_BUDDY_GUARD

# Compose binds to one IPv4 address, so Docker normally has no IPv6 listener.
# If Docker has an IPv6 DOCKER-USER chain, protect it as defense in depth. IPv6
# HTTPS is denied unless an explicit trusted IPv6 subnet is configured.
if command -v ip6tables >/dev/null 2>&1 \
  && ip6tables -w 10 -n -L DOCKER-USER >/dev/null 2>&1; then
  ip6tables -w 10 -N MAIL_BUDDY_GUARD6 2>/dev/null || true
  ip6tables -w 10 -F MAIL_BUDDY_GUARD6
  ip6tables -w 10 -A MAIL_BUDDY_GUARD6 -p tcp --dport 443 -j DROP
  ip6tables -w 10 -A MAIL_BUDDY_GUARD6 -j RETURN
  ip6tables -w 10 -C DOCKER-USER -i "$lan_interface" \
    -j MAIL_BUDDY_GUARD6 2>/dev/null \
    || ip6tables -w 10 -I DOCKER-USER 1 -i "$lan_interface" \
      -j MAIL_BUDDY_GUARD6

  ip6tables -w 10 -N MAIL_BUDDY_LAN6 2>/dev/null || true
  ip6tables -w 10 -F MAIL_BUDDY_LAN6
  ip6tables -w 10 -A MAIL_BUDDY_LAN6 -p tcp --dport 443 -j DROP
  if [ -n "$lan_ipv6_subnet" ]; then
    ip6tables -w 10 -I MAIL_BUDDY_LAN6 1 \
      -s "$lan_ipv6_subnet" -p tcp --dport 443 -j RETURN
  fi
  ip6tables -w 10 -I MAIL_BUDDY_LAN6 1 \
    -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  ip6tables -w 10 -A MAIL_BUDDY_LAN6 -j RETURN
  ip6tables -w 10 -C DOCKER-USER -i "$lan_interface" \
    -j MAIL_BUDDY_LAN6 2>/dev/null \
    || ip6tables -w 10 -I DOCKER-USER 1 -i "$lan_interface" \
      -j MAIL_BUDDY_LAN6
  ip6tables -w 10 -D DOCKER-USER -i "$lan_interface" \
    -j MAIL_BUDDY_GUARD6
fi

echo "Docker TCP/443 is now limited to $lan_subnet on $lan_interface."
if [ -n "$lan_ipv6_subnet" ]; then
  echo "Docker IPv6 TCP/443 is limited to $lan_ipv6_subnet when IPv6 is enabled."
else
  echo "Docker IPv6 TCP/443 is denied when Docker IPv6 is enabled."
fi
echo "Install deploy/mail-buddy-firewall.service to reapply this rule after boot."
