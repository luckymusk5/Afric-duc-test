import base64
import hashlib
import hmac
import json
import math
import os
import psycopg
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PORT = int(os.getenv('QR_SERVICE_PORT', '8081'))
SERVICE_TOKEN = os.getenv('QR_SERVICE_TOKEN', '')
STEP_SECONDS = 15
MAX_BODY_BYTES = 4096
DB_HOST = os.getenv('POSTGRES_HOST', 'db')
DB_NAME = os.getenv('POSTGRES_DB', 'passage')
DB_USER = os.getenv('QR_DB_USER', '')
DB_PASSWORD = os.getenv('QR_DB_PASSWORD', '')
DB_PORT = int(os.getenv('POSTGRES_PORT', '5432'))
ENCODED_BADGE_KEY = os.getenv('BADGE_SECRET_KEY', '')

if len(SERVICE_TOKEN) < 32:
    raise RuntimeError('QR_SERVICE_TOKEN must contain at least 32 characters.')
if not DB_USER or len(DB_PASSWORD) < 32:
    raise RuntimeError('QR database read-only credentials are required.')
try:
    BADGE_SECRET_KEY = base64.urlsafe_b64decode(
        ENCODED_BADGE_KEY + '=' * (-len(ENCODED_BADGE_KEY) % 4),
    )
except (ValueError, UnicodeEncodeError) as error:
    raise RuntimeError('BADGE_SECRET_KEY is invalid.') from error
if len(BADGE_SECRET_KEY) != 32:
    raise RuntimeError('BADGE_SECRET_KEY must decode to exactly 32 bytes.')


def active_badge_for_user(user_id):
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError('Invalid user id.')
    with psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=2,
        options='-c default_transaction_read_only=on',
    ) as database:
        row = database.execute(
            'SELECT user_id, identifier, user_role, created_at, student_name, '
            'class_name, school, student_active, badge_id, badge_secret '
            'FROM public.qr_active_student_badge(%s)',
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    if row[2] != 'student' or row[7] is not True:
        return None
    badge_id = str(row[8])
    try:
        version, encoded = row[9].split(':', 1)
        if version != 'v1':
            return None
        ciphertext = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        secret = AESGCM(BADGE_SECRET_KEY).decrypt(
            ciphertext[:12],
            ciphertext[12:],
            b'afric-educ.badge-secret.v1:' + uuid.UUID(badge_id).bytes,
        )
    except (AttributeError, TypeError, ValueError, InvalidTag, UnicodeEncodeError):
        return None
    if len(secret) != 32:
        return None
    return badge_id, secret


def generate_token(badge_id, secret, unix_seconds):
    if not isinstance(badge_id, str):
        raise ValueError('Invalid badge id.')
    try:
        badge_uuid = uuid.UUID(badge_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError('Invalid badge id.') from error
    if str(badge_uuid) != badge_id:
        raise ValueError('Invalid badge id.')
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise ValueError('Invalid badge secret.')
    if isinstance(unix_seconds, bool) or not isinstance(unix_seconds, (int, float)):
        raise ValueError('Invalid timestamp.')
    if not math.isfinite(unix_seconds) or unix_seconds <= 0:
        raise ValueError('Invalid timestamp.')

    counter = int(unix_seconds // STEP_SECONDS)
    message = f'{badge_id}.{counter}'.encode('ascii')
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, message, hashlib.sha256).digest()[:16],
    ).rstrip(b'=').decode('ascii')
    return {
        'badge_id': badge_id,
        'token': f'{badge_id}.{counter}.{signature}',
        'counter': counter,
        'expires_at': (counter + 1) * STEP_SECONDS,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'StudentQrService'

    def _send_json(self, status, payload):
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/healthz':
            self._send_json(200, {'status': 'ok'})
            return
        self._send_json(404, {'detail': 'Not found.'})

    def do_POST(self):
        if self.path != '/v1/token':
            self._send_json(404, {'detail': 'Not found.'})
            return

        supplied = self.headers.get('Authorization', '')
        expected = f'Bearer {SERVICE_TOKEN}'
        if not hmac.compare_digest(supplied, expected):
            self._send_json(401, {'detail': 'Unauthorized.'})
            return

        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError('Invalid request size.')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid request.')
            badge = active_badge_for_user(payload.get('user_id'))
            if badge is None:
                self._send_json(404, {'detail': 'No active student badge.'})
                return
            result = generate_token(
                badge[0],
                badge[1],
                payload.get('unix_seconds'),
            )
        except psycopg.Error:
            self._send_json(503, {'detail': 'Student database temporarily unavailable.'})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
            self._send_json(400, {'detail': 'Invalid request.'})
            return
        self._send_json(200, result)

    def log_message(self, format, *args):
        # Never log request bodies, signatures, or authorization headers.
        super().log_message(format, *args)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    server.serve_forever()
