import base64
import binascii
import ipaddress
import math
import socket
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from .db import connect


RETRY_SECONDS = (30, 120, 600, 3600, 21600)
LIFETIME = timedelta(hours=24)


def vapid_public_key(private_key_path):
    key = Vapid02.from_file(private_key_file=str(private_key_path)).public_key
    raw = key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def valid_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    try:
        url = urlsplit(endpoint)
        if url.scheme != "https" or url.username or url.password or url.fragment or url.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = url.hostname or ""
    if not (host == "fcm.googleapis.com" or host == "updates.push.services.mozilla.com" or
            host == "web.push.apple.com" or host.endswith(".push.apple.com")):
        return False
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (OSError, ValueError):
        return False


def valid_keys(p256dh, auth):
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        return False
    try:
        public = base64.b64decode(p256dh + "=" * (-len(p256dh) % 4), altchars=b"-_", validate=True)
        secret = base64.b64decode(auth + "=" * (-len(auth) % 4), altchars=b"-_", validate=True)
        if len(public) != 65 or public[0] != 4 or len(secret) != 16:
            return False
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except (binascii.Error, ValueError):
        return False
    return True


def valid_subscription(data):
    if not isinstance(data, dict) or not {"endpoint", "keys"} <= set(data) or \
       set(data) - {"endpoint", "keys", "previous_id"} or not valid_endpoint(data["endpoint"]):
        return False
    keys = data["keys"]
    return isinstance(keys, dict) and set(keys) == {"p256dh", "auth"} and \
        valid_keys(keys["p256dh"], keys["auth"])


class NoRedirectSession(requests.Session):
    def post(self, url, **kwargs):
        return super().post(url, allow_redirects=False, **kwargs)


def send_push(private_key, subject, item, ttl):
    with NoRedirectSession() as session:
        session.trust_env = False
        try:
            webpush({"endpoint": item["endpoint"],
                     "keys": {"p256dh": item["p256dh"], "auth": item["auth"]}},
                    data=str(item["run_id"]), vapid_private_key=str(private_key),
                    vapid_claims={"sub": subject}, ttl=ttl, timeout=10, requests_session=session)
            return 201, None
        except WebPushException as exc:
            response = exc.response
            return (response.status_code, response.headers.get("Retry-After")) if response is not None else (0, None)
        except (requests.RequestException, OSError):
            return 0, None


def retry_after_seconds(value):
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, math.ceil((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def send_due(app):
    with connect(app.dsn) as conn:
        conn.execute("""DELETE FROM notification_deliveries WHERE state IN ('accepted','abandoned')
                        AND completed_at < now()-interval '7 days'""")
        conn.execute("""UPDATE notification_deliveries d SET state='abandoned',completed_at=now()
                        FROM events e WHERE d.event_id=e.id AND d.state='pending'
                        AND e.created_at + interval '24 hours' <= now()""")
        item = conn.execute("""SELECT d.event_id,d.subscription_id,d.attempt_count,e.run_id,e.created_at,
                              s.endpoint,s.p256dh,s.auth FROM notification_deliveries d
                              JOIN events e ON e.id=d.event_id
                              JOIN push_subscriptions s ON s.id=d.subscription_id
                              WHERE d.state='pending' AND d.next_attempt_at<=now()
                              ORDER BY d.next_attempt_at,d.event_id FOR UPDATE OF d SKIP LOCKED LIMIT 1""").fetchone()
        if not item:
            return False
        count = item["attempt_count"] + 1
        delay = RETRY_SECONDS[count - 1] if count <= 5 else 86400
        conn.execute("""UPDATE notification_deliveries SET attempt_count=%s,
                        next_attempt_at=now()+(%s || ' seconds')::interval
                        WHERE event_id=%s AND subscription_id=%s""",
                     (count, delay, item["event_id"], item["subscription_id"]))
    remaining = max(0, math.ceil(((item["created_at"] + LIFETIME) - datetime.now(timezone.utc)).total_seconds()))
    if not valid_keys(item["p256dh"], item["auth"]):
        with connect(app.dsn) as conn:
            conn.execute("""DELETE FROM push_subscriptions WHERE id=%s AND p256dh=%s AND auth=%s""",
                         (item["subscription_id"], item["p256dh"], item["auth"]))
        return True
    if not remaining:
        status, retry_after = 0, None
    elif not valid_endpoint(item["endpoint"]):
        status, retry_after = 400, None
    else:
        status, retry_after = send_push(app.vapid_private_key, app.vapid_subject, item, remaining)
    with connect(app.dsn) as conn:
        if status in (404, 410):
            conn.execute("DELETE FROM push_subscriptions WHERE id=%s", (item["subscription_id"],))
        elif 200 <= status <= 202:
            conn.execute("""UPDATE notification_deliveries SET state='accepted',completed_at=now()
                            WHERE event_id=%s AND subscription_id=%s AND state='pending'""",
                         (item["event_id"], item["subscription_id"]))
        elif status == 0 or status == 429 or 500 <= status < 600:
            if count >= 6 or remaining <= 0:
                conn.execute("""UPDATE notification_deliveries SET state='abandoned',completed_at=now()
                                WHERE event_id=%s AND subscription_id=%s AND state='pending'""",
                             (item["event_id"], item["subscription_id"]))
            else:
                delay = max(RETRY_SECONDS[count - 1], retry_after_seconds(retry_after))
                conn.execute("""UPDATE notification_deliveries SET next_attempt_at=LEAST(
                                (SELECT created_at + interval '24 hours' FROM events WHERE id=%s),
                                now()+(%s || ' seconds')::interval)
                                WHERE event_id=%s AND subscription_id=%s AND state='pending'""",
                             (item["event_id"], delay, item["event_id"], item["subscription_id"]))
        else:
            conn.execute("""UPDATE notification_deliveries SET state='abandoned',completed_at=now()
                            WHERE event_id=%s AND subscription_id=%s AND state='pending'""",
                         (item["event_id"], item["subscription_id"]))
    return True


def sender_loop(app):
    while not app.stopping.is_set():
        try:
            if send_due(app):
                continue
        except Exception:
            # Delivery never controls workflow progress; retry on the next pass.
            pass
        app.stopping.wait(2)
