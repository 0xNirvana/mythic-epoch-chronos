#!/usr/bin/env python3
import mythic_container
from mythic.epoch import *

# Mythic automatically launches the C2 server via server_binary_path
# defined in the Epoch C2 profile class. Do NOT start it manually here
# to avoid duplicate server instances competing for calendar events.

if __name__ == "__main__":
    mythic_container.mythic_service.start_and_run_forever()
