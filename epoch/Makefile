# Chronos Makefile

.PHONY: help install uninstall start stop logs test clean

help:
	@echo "Chronos - Google Calendar Dead Drop C2 Profile"
	@echo ""
	@echo "Available targets:"
	@echo "  install   - Install Chronos into Mythic"
	@echo "  start     - Start Chronos container"
	@echo "  stop      - Stop Chronos container"
	@echo "  logs      - View Chronos container logs"
	@echo "  shell     - Open shell in Chronos container"
	@echo "  clean     - Remove OAuth tokens and cached data"
	@echo "  test      - Test Google Calendar API connection"

install:
	@echo "[*] Installing Chronos..."
	cd ../../ && sudo ./mythic-cli install folder $(PWD)
	@echo "[+] Chronos installed!"

start:
	@echo "[*] Starting Chronos..."
	cd ../../ && sudo ./mythic-cli start epoch
	@echo "[+] Chronos started!"

stop:
	@echo "[*] Stopping Chronos..."
	cd ../../ && sudo ./mythic-cli stop epoch
	@echo "[+] Chronos stopped!"

logs:
	@echo "[*] Viewing Chronos logs..."
	sudo docker logs -f epoch

shell:
	@echo "[*] Opening shell in Chronos container..."
	sudo docker exec -it epoch /bin/bash

clean:
	@echo "[*] Cleaning OAuth tokens and cache..."
	sudo docker exec -it epoch rm -f /Mythic/epoch/c2_code/token.pickle
	sudo docker exec -it epoch rm -f /Mythic/epoch/c2_code/__pycache__/*
	@echo "[+] Cleaned!"

test:
	@echo "[*] Testing Google Calendar API connection..."
	sudo docker exec -it epoch python3 /Mythic/epoch/c2_code/server.py --test
