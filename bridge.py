"""
Sinsera Garage Pi Bridge.

Connects to an OBDLink MX+ over BLE, drives the ELM327 command set,
samples a fixed set of PIDs at SAMPLE_HZ, polls DTCs every
DTC_POLL_SECS, and POSTs readings + DTC snapshots to Supabase under
the authenticated user's session.

Designed to be the only process on a DietPi-based Pi 5 dedicated to
this purpose. Runs as a systemd service (see garage-obd-bridge.service).
"""

import asyncio
import os
import time
import json
import signal
import logging
from collections import deque
from datetime import datetime, timezone

import aiohttp
from bleak import BleakClient, BleakScanner
from dotenv import load_dotenv

from obd_pids import PID_MAP, parse_dtc_response

load_dotenv('/opt/garage-pi-bridge/.env')

SUPABASE_URL      = os.environ['SUPABASE_URL']
SUPABASE_ANON_KEY = os.environ['SUPABASE_ANON_KEY']
SUPABASE_EMAIL    = os.environ['SUPABASE_EMAIL']
SUPABASE_PASSWORD = os.environ['SUPABASE_PASSWORD']
CAR_ID            = os.environ['CAR_ID']
OBDLINK_MAC       = os.environ.get('OBDLINK_MAC') or None
OBDLINK_NAME      = os.environ.get('OBDLINK_NAME', 'OBDLink MX+')
SAMPLE_HZ         = float(os.environ.get('SAMPLE_HZ', '5'))
DTC_POLL_SECS     = float(os.environ.get('DTC_POLL_SECS', '10'))
BATCH_SIZE        = int(os.environ.get('BATCH_SIZE', '10'))

# OBDLink MX+ exposes ELM327 over a custom BLE service. The MX+ docs
# don't pin the UUIDs publicly; we discover them at runtime by scanning
# for writable + notifyable characteristics on the service that doesn't
# match standard GATT services (battery, device info, etc.). One
# characteristic is write-without-response (TX from us), the other is
# notify (RX from MX+).

# Configure logging — single-line, timestamped, journalctl-friendly.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('bridge')


# ── Supabase ────────────────────────────────────────────────────────────────

class Supabase:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.access_token = None
        self.user_id = None
        self.session_id = None

    async def login(self):
        url = f'{SUPABASE_URL}/auth/v1/token?grant_type=password'
        async with self.s.post(url, json={
            'email': SUPABASE_EMAIL, 'password': SUPABASE_PASSWORD,
        }, headers={'apikey': SUPABASE_ANON_KEY}) as r:
            r.raise_for_status()
            data = await r.json()
            self.access_token = data['access_token']
            self.user_id = data['user']['id']
            log.info(f'authenticated as {self.user_id[:8]}…')

    def _auth_headers(self):
        return {
            'apikey': SUPABASE_ANON_KEY,
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json',
        }

    async def open_session(self, protocol: str | None = None, vin: str | None = None):
        url = f'{SUPABASE_URL}/rest/v1/garage_obd_sessions'
        body = [{
            'car_id': CAR_ID, 'user_id': self.user_id,
            'source': 'pi-bridge', 'device_label': OBDLINK_NAME,
            'protocol': protocol, 'vin': vin,
        }]
        async with self.s.post(url, json=body, headers={
            **self._auth_headers(), 'Prefer': 'return=representation',
        }) as r:
            r.raise_for_status()
            row = (await r.json())[0]
            self.session_id = row['id']
            log.info(f'session {self.session_id[:8]}… opened against car {CAR_ID[:8]}…')

    async def close_session(self):
        if not self.session_id:
            return
        url = f"{SUPABASE_URL}/rest/v1/garage_obd_sessions?id=eq.{self.session_id}"
        async with self.s.patch(url, json={
            'ended_at': datetime.now(timezone.utc).isoformat(),
        }, headers=self._auth_headers()) as r:
            if r.status >= 400:
                log.warning(f'failed to close session ({r.status})')

    async def push_readings(self, rows):
        if not rows: return
        url = f'{SUPABASE_URL}/rest/v1/garage_obd_readings'
        for row in rows:
            row['session_id'] = self.session_id
        async with self.s.post(url, json=rows, headers=self._auth_headers()) as r:
            if r.status >= 400:
                body = await r.text()
                log.warning(f'push_readings {r.status}: {body[:160]}')

    async def push_dtcs(self, codes, status):
        if not codes: return
        url = f'{SUPABASE_URL}/rest/v1/garage_obd_dtcs'
        body = [{
            'session_id': self.session_id, 'code': c, 'status': status,
        } for c in codes]
        async with self.s.post(url, json=body, headers={
            **self._auth_headers(), 'Prefer': 'resolution=merge-duplicates',
        }) as r:
            if r.status >= 400:
                t = await r.text()
                log.warning(f'push_dtcs {r.status}: {t[:160]}')


# ── ELM327 over BLE ─────────────────────────────────────────────────────────

class ELM327:
    """Frame ELM/STN commands and parse line-terminated responses."""

    def __init__(self, client: BleakClient, write_uuid: str, notify_uuid: str):
        self.client = client
        self.write_uuid = write_uuid
        self.notify_uuid = notify_uuid
        self.rx_buf = bytearray()
        self.pending: asyncio.Future | None = None

    async def start(self):
        await self.client.start_notify(self.notify_uuid, self._on_data)

    def _on_data(self, _sender, data: bytearray):
        self.rx_buf.extend(data)
        # Responses terminate with '>' (the ELM prompt).
        if b'>' in self.rx_buf:
            line = bytes(self.rx_buf).decode(errors='ignore')
            self.rx_buf.clear()
            if self.pending and not self.pending.done():
                self.pending.set_result(line)

    async def cmd(self, s: str, timeout: float = 1.0) -> str:
        # Send a single command, await one ELM-terminated response.
        self.pending = asyncio.get_event_loop().create_future()
        await self.client.write_gatt_char(self.write_uuid, (s + '\r').encode(), response=False)
        try:
            return await asyncio.wait_for(self.pending, timeout=timeout)
        except asyncio.TimeoutError:
            return ''

    async def init(self):
        # Reset, kill echo + linefeeds, auto-protocol detect, headers on.
        for c in ('ATZ', 'ATE0', 'ATL0', 'ATSP0', 'ATH1', 'ATS0', 'ATAT1'):
            await self.cmd(c, timeout=2.0)

    async def read_pid(self, pid_hex: str) -> bytes | None:
        # Mode + PID (e.g. '010C'). Response strips spaces + the echo
        # of the mode-byte (e.g. '41 0C ...'), leaving the data bytes.
        raw = await self.cmd(pid_hex)
        if not raw or 'NO DATA' in raw or '?' in raw:
            return None
        # Take the last non-empty line that starts with '41' (response
        # mode = request mode + 0x40). Some queries return multi-line
        # ISO-TP frames; we use the FIRST data frame here.
        for line in raw.splitlines():
            cleaned = line.replace(' ', '').replace('\r', '').upper()
            if cleaned.startswith('41'):
                # Drop '41' + the PID echo, return the rest as bytes.
                hexstr = cleaned[2 + len(pid_hex) - 2:]
                try:
                    return bytes.fromhex(hexstr)
                except ValueError:
                    return None
        return None

    async def read_dtcs(self, mode: str = '03') -> list[str]:
        # Modes: 03 = stored, 07 = pending, 0A = permanent.
        raw = await self.cmd(mode, timeout=2.0)
        # Collect every line that looks like '43 ...' or '47 ...' etc.
        bytestream = bytearray()
        prefix = f'4{mode[1]}'
        for line in raw.splitlines():
            cleaned = line.replace(' ', '').replace('\r', '').upper()
            if cleaned.startswith(prefix):
                try:
                    bytestream.extend(bytes.fromhex(cleaned[2:]))
                except ValueError:
                    continue
        return parse_dtc_response(bytes(bytestream))


# ── BLE discovery ──────────────────────────────────────────────────────────

async def discover_obdlink_uuids(client: BleakClient):
    """
    Walk the GATT tree and return (write_uuid, notify_uuid) for the
    one custom service that has both a writable + notifyable
    characteristic. OBDLink MX+ presents exactly one such service.
    """
    services = client.services
    for svc in services:
        # Skip well-known services (Battery, Generic Access, etc.).
        if svc.uuid.startswith('0000180') or svc.uuid.startswith('00001801'):
            continue
        write_c = None
        notify_c = None
        for ch in svc.characteristics:
            props = ch.properties
            if ('write' in props or 'write-without-response' in props) and write_c is None:
                write_c = ch.uuid
            if 'notify' in props and notify_c is None:
                notify_c = ch.uuid
        if write_c and notify_c:
            return write_c, notify_c
    return None, None


# ── Main loop ──────────────────────────────────────────────────────────────

async def find_obdlink():
    if OBDLINK_MAC:
        return OBDLINK_MAC
    log.info(f'scanning for "{OBDLINK_NAME}" (no MAC pinned in .env)…')
    dev = await BleakScanner.find_device_by_filter(
        lambda d, _ad: (d.name or '').lower().startswith(OBDLINK_NAME.lower()),
        timeout=15.0,
    )
    if not dev:
        return None
    return dev.address


async def run_once():
    mac = await find_obdlink()
    if not mac:
        log.error('OBDLink not found')
        return False

    async with BleakClient(mac) as ble:
        if not ble.is_connected:
            log.error('failed to connect over BLE')
            return False
        log.info(f'connected to {mac}')

        write_uuid, notify_uuid = await discover_obdlink_uuids(ble)
        if not write_uuid or not notify_uuid:
            log.error('could not discover write+notify characteristics')
            return False

        elm = ELM327(ble, write_uuid, notify_uuid)
        await elm.start()
        await elm.init()
        log.info('ELM327 initialised')

        async with aiohttp.ClientSession() as http:
            sb = Supabase(http)
            await sb.login()
            await sb.open_session(protocol='auto')

            buffer = deque()
            last_dtc_poll = 0.0
            interval = 1.0 / SAMPLE_HZ

            try:
                while True:
                    t0 = time.monotonic()
                    row = {'sampled_at': datetime.now(timezone.utc).isoformat()}
                    pids_extra = {}
                    for pid_hex, (col, decode) in PID_MAP.items():
                        raw = await elm.read_pid(pid_hex)
                        if raw is None:
                            continue
                        val = decode(raw)
                        if val is None:
                            continue
                        if col == 'rpm':
                            row[col] = int(val)
                        else:
                            row[col] = val
                    if 'pids' not in row:
                        row['pids'] = pids_extra
                    buffer.append(row)

                    if len(buffer) >= BATCH_SIZE:
                        await sb.push_readings(list(buffer))
                        buffer.clear()

                    if time.monotonic() - last_dtc_poll > DTC_POLL_SECS:
                        last_dtc_poll = time.monotonic()
                        for mode, status in (('03', 'active'), ('07', 'pending'), ('0A', 'permanent')):
                            codes = await elm.read_dtcs(mode)
                            if codes:
                                log.info(f'DTCs ({status}): {", ".join(codes)}')
                                await sb.push_dtcs(codes, status)

                    elapsed = time.monotonic() - t0
                    sleep_for = max(0, interval - elapsed)
                    await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                log.info('shutting down…')
            finally:
                if buffer:
                    await sb.push_readings(list(buffer))
                await sb.close_session()
    return True


async def main():
    # Auto-reconnect loop — if BLE drops (car off, distance, etc.),
    # back off and try again. Don't spin tightly.
    backoff = 5
    while True:
        try:
            ok = await run_once()
            backoff = 5 if ok else min(backoff * 2, 120)
        except Exception as e:
            log.exception(f'run failed: {e}')
            backoff = min(backoff * 2, 120)
        log.info(f'sleeping {backoff}s before retry…')
        await asyncio.sleep(backoff)


def _install_signal_handlers(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in asyncio.all_tasks(loop)])


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
