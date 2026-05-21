"""
OBD-II PID definitions + decoders.

A PID is a 2-byte address (mode + parameter). The standard OBD-II set
is defined in SAE J1979; what's here covers the "hot" PIDs the bridge
samples at 5 Hz. The decoder takes the raw response bytes and returns
either a number, an int, or None (when the ECU returned a no-data /
bad-response).
"""


def _coolant(b):
    # 0105 — A−40, deg C, single byte.
    if len(b) < 1: return None
    return b[0] - 40


def _rpm(b):
    # 010C — (256*A + B) / 4
    if len(b) < 2: return None
    return ((b[0] << 8) | b[1]) / 4


def _speed(b):
    # 010D — A, km/h
    if len(b) < 1: return None
    return b[0]


def _iat(b):
    # 010F — A−40, deg C
    if len(b) < 1: return None
    return b[0] - 40


def _maf(b):
    # 0110 — (256*A + B) / 100, grams/sec
    if len(b) < 2: return None
    return ((b[0] << 8) | b[1]) / 100.0


def _throttle(b):
    # 0111 — A * 100/255, percent
    if len(b) < 1: return None
    return round(b[0] * 100 / 255, 2)


def _load(b):
    # 0104 — A * 100/255, percent
    if len(b) < 1: return None
    return round(b[0] * 100 / 255, 2)


def _fuel(b):
    # 012F — A * 100/255, percent
    if len(b) < 1: return None
    return round(b[0] * 100 / 255, 2)


def _short_ft(b):
    # 0106 — (A − 128) * 100/128, percent
    if len(b) < 1: return None
    return round((b[0] - 128) * 100 / 128, 2)


def _long_ft(b):
    # 0107 — same scale
    if len(b) < 1: return None
    return round((b[0] - 128) * 100 / 128, 2)


def _battery(b):
    # 0142 — Control module voltage. (256*A + B) / 1000, volts.
    if len(b) < 2: return None
    return round(((b[0] << 8) | b[1]) / 1000.0, 2)


# pid_hex -> (column-name on garage_obd_readings, decoder)
PID_MAP = {
    '0104': ('engine_load',  _load),
    '0105': ('coolant_c',    _coolant),
    '0106': ('short_ft_b1',  _short_ft),
    '0107': ('long_ft_b1',   _long_ft),
    '010C': ('rpm',          _rpm),
    '010D': ('speed_kph',    _speed),
    '010F': ('iat_c',        _iat),
    '0110': ('maf_gps',      _maf),
    '0111': ('throttle_pct', _throttle),
    '012F': ('fuel_lvl_pct', _fuel),
    '0142': ('battery_v',    _battery),
}


def parse_dtc_response(payload):
    """
    Mode 03 / 07 / 0A response carries a count byte + N×2-byte DTCs.
    Each 2-byte DTC encodes:
      bits 15–14  -> letter  (00=P, 01=C, 10=B, 11=U)
      bits 13–12  -> first digit  (0..3)
      bits 11–8   -> second digit (hex nibble)
      bits 7–4    -> third digit
      bits 3–0    -> fourth digit
    Returns list of formatted codes like 'P0420'.
    """
    if not payload or len(payload) < 1:
        return []
    count = payload[0]
    out = []
    for i in range(count):
        off = 1 + i * 2
        if off + 1 >= len(payload):
            break
        a, b = payload[off], payload[off + 1]
        letter = 'PCBU'[(a >> 6) & 0b11]
        d1 = (a >> 4) & 0b11
        d2 = a & 0b1111
        d3 = (b >> 4) & 0b1111
        d4 = b & 0b1111
        code = f'{letter}{d1}{d2:X}{d3:X}{d4:X}'
        if code != 'P0000':         # padding / no-fault
            out.append(code)
    return out
