# -*- coding: utf-8 -*-
# Copyright 2014 Leon Handreke
# Copyright 2017 New Vector Ltd
# Copyright 2019-2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import re

from firebase_admin import messaging
from prometheus_client import Counter, Gauge, Histogram
from twisted.enterprise.adbapi import ConnectionPool
from twisted.internet.defer import DeferredSemaphore
from twisted.web.client import HTTPConnectionPool

from sygnal.helper.context_factory import ClientTLSOptionsFactory
from sygnal.helper.proxy.proxyagent_twisted import ProxyAgent
from sygnal.utils import NotificationLoggerAdapter

from .exceptions import PushkinSetupException
from .notifications import ConcurrencyLimitedPushkin

QUEUE_TIME_HISTOGRAM = Histogram(
    "sygnal_gcm_queue_time", "Time taken waiting for a connection to GCM"
)

SEND_TIME_HISTOGRAM = Histogram(
    "sygnal_gcm_request_time", "Time taken to send HTTP request to GCM"
)

PENDING_REQUESTS_GAUGE = Gauge(
    "sygnal_pending_gcm_requests", "Number of GCM requests waiting for a connection"
)

ACTIVE_REQUESTS_GAUGE = Gauge(
    "sygnal_active_gcm_requests", "Number of GCM requests in flight"
)

RESPONSE_STATUS_CODES_COUNTER = Counter(
    "sygnal_gcm_status_codes",
    "Number of HTTP response status codes received from GCM",
    labelnames=["pushkin", "code"],
)

logger = logging.getLogger(__name__)

GCM_URL = b"https://fcm.googleapis.com/fcm/send"
MAX_TRIES = 3
RETRY_DELAY_BASE = 10
MAX_BYTES_PER_FIELD = 1024

# The error codes that mean a registration ID will never
# succeed and we should reject it upstream.
# We include NotRegistered here too for good measure, even
# though gcm-client "helpfully" extracts these into a separate
# list.
BAD_PUSHKEY_FAILURE_CODES = [
    "MissingRegistration",
    "InvalidRegistration",
    "NotRegistered",
    "InvalidPackageName",
    "MismatchSenderId",
]

# Failure codes that mean the message in question will never
# succeed, so don"t retry, but the registration ID is fine
# so we should not reject it upstream.
BAD_MESSAGE_FAILURE_CODES = ["MessageTooBig", "InvalidDataKey", "InvalidTtl"]

DEFAULT_MAX_CONNECTIONS = 20


class GcmPushkin(ConcurrencyLimitedPushkin):
    """
    Pushkin that relays notifications to Google/Firebase Cloud Messaging.
    """

    UNDERSTOOD_CONFIG_FIELDS = {
        "type",
        "api_key",
        "fcm_options",
        "max_connections",
    } | ConcurrencyLimitedPushkin.UNDERSTOOD_CONFIG_FIELDS

    def __init__(self, name, sygnal, config, canonical_reg_id_store):
        super(GcmPushkin, self).__init__(name, sygnal, config)

        nonunderstood = set(self.cfg.keys()).difference(self.UNDERSTOOD_CONFIG_FIELDS)
        if len(nonunderstood) > 0:
            logger.warning(
                "The following configuration fields are not understood: %s",
                nonunderstood,
            )

        self.http_pool = HTTPConnectionPool(reactor=sygnal.reactor)
        self.max_connections = self.get_config(
            "max_connections", DEFAULT_MAX_CONNECTIONS
        )
        self.connection_semaphore = DeferredSemaphore(self.max_connections)
        self.http_pool.maxPersistentPerHost = self.max_connections

        tls_client_options_factory = ClientTLSOptionsFactory()

        # use the Sygnal global proxy configuration
        proxy_url = sygnal.config.get("proxy")

        self.http_agent = ProxyAgent(
            reactor=sygnal.reactor,
            pool=self.http_pool,
            contextFactory=tls_client_options_factory,
            proxy_url_str=proxy_url,
        )

        self.db = sygnal.database
        self.canonical_reg_id_store = canonical_reg_id_store

        self.api_key = self.get_config("api_key")
        if not self.api_key:
            raise PushkinSetupException("No API key set in config")

        # Use the fcm_options config dictionary as a foundation for the body;
        # this lets the Sygnal admin choose custom FCM options
        # (e.g. content_available).
        self.base_request_body: dict = self.get_config("fcm_options", {})
        if not isinstance(self.base_request_body, dict):
            raise PushkinSetupException(
                "Config field fcm_options, if set, must be a dictionary of options"
            )

    @classmethod
    async def create(cls, name, sygnal, config):
        """
        Override this if your pushkin needs to call async code in order to
        be constructed. Otherwise, it defaults to just invoking the Python-standard
        __init__ constructor.

        Returns:
            an instance of this Pushkin
        """
        logger.debug("About to set up CanonicalRegId Store")
        canonical_reg_id_store = CanonicalRegIdStore(
            sygnal.database, sygnal.database_engine
        )
        await canonical_reg_id_store.setup()
        logger.debug("Finished setting up CanonicalRegId Store")

        return cls(name, sygnal, config, canonical_reg_id_store)

    async def _dispatch_notification_unlimited(self, n, device, context):
        log = NotificationLoggerAdapter(logger, {"request_id": context.request_id})

        log.info(f"Start dispatch inside gcmpushkin")

        pushkeys = [
            device.pushkey for device in n.devices if device.app_id == self.name
        ]
        # Resolve canonical IDs for all pushkeys

        if pushkeys[0] != device.pushkey:
            log.info(f"Only send notifications once, to all devices at once. Return")
            # Only send notifications once, to all devices at once.
            return []

        # The pushkey is kind of secret because you can use it to send push
        # to someone.
        # span_tags = {"pushkeys": pushkeys}
        span_tags = {"gcm_num_devices": len(pushkeys)}

        with self.sygnal.tracer.start_span(
            "gcm_dispatch", tags=span_tags, child_of=context.opentracing_span
        ) as span_parent:
            reg_id_mappings = await self.canonical_reg_id_store.get_canonical_ids(
                pushkeys
            )

            reg_id_mappings = {
                reg_id: canonical_reg_id or reg_id
                for (reg_id, canonical_reg_id) in reg_id_mappings.items()
            }

            data = GcmPushkin._build_data(n, device)

            if not data.get("event_id"):
                log.info(f"Event id is empty")
                return []

            # count the number of remapped registration IDs in the request
            span_parent.set_tag(
                "gcm_num_remapped_reg_ids_used",
                [k != v for (k, v) in reg_id_mappings.items()].count(True),
            )

            mapped_push_keys = [reg_id_mappings[pk] for pk in pushkeys]

            if data.get("room_alias") and "/hidden" in data.get("room_alias"):
                log.info(f"Room alias is hidden")
                return []

            message = GcmPushkin._build_message(data, n.prio)

            if len(pushkeys) == 1:
                message.token = mapped_push_keys[0]
            else:
                message.tokens = mapped_push_keys

            log.info(f"Get message => {json.dumps(data)}")

            try:
                response = messaging.send(message)
            except Exception as e:
                response = "error"

            print("Successfully sent message:", response)

            return []

    @staticmethod
    def _build_data(n, device):
        """
        Build the payload data to be sent.
        Args:
            n: Notification to build the payload for.
            device (Device): Device information to which the constructed payload
            will be sent.

        Returns:
            JSON-compatible dict
        """
        data = {}

        if device.data:
            data.update(device.data.get("default_payload", {}))

        for attr in [
            "event_id",
            "type",
            "sender",
            "room_name",
            "room_alias",
            "call_id",
            "membership",
            "sender_display_name",
            "content",
            "room_id",
        ]:
            if hasattr(n, attr):
                data[attr] = getattr(n, attr)
                # Truncate fields to a sensible maximum length. If the whole
                # body is too long, GCM will reject it.
                if data[attr] is not None and len(data[attr]) > MAX_BYTES_PER_FIELD:
                    data[attr] = data[attr][0:MAX_BYTES_PER_FIELD]

        data["prio"] = "high"
        if n.prio == "low":
            data["prio"] = "normal"

        if getattr(n, "counts", None):
            data["unread"] = n.counts.unread
            data["missed_calls"] = n.counts.missed_calls

        return data

    @staticmethod
    def _build_message(data, prio):

        # data=data,
        message = messaging.Message(
            data={
                "room_id": data.get("room_id"),
                "room_name": data.get("room_name"),
            },
            notification=messaging.Notification(),
            android=messaging.AndroidConfig(
                collapse_key=data.get("room_id"),
                notification=messaging.AndroidNotification(
                    notification_count=data.get("unread"),
                    tag=data.get("room_id"),
                    priority="high",
                    default_vibrate_timings=True,
                    sound="default",
                    default_light_settings=True,
                    visibility="public",
                ),
                priority="normal" if prio == "low" else "high",
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        content_available=1,
                        thread_id=data.get("room_id"),
                        badge=data.get("unread"),
                    )
                ),
            ),
        )

        sender_display_name = ""

        if data.get("sender_display_name"):
            sender_display_name = data.get("sender_display_name")

        message.notification.body = sender_display_name

        if data.get("room_name") and data.get("room_name").startswith("@"):
            message.notification.title = data.get("room_name")
        else:
            if data.get("sender_display_name"):
                message.notification.title = sender_display_name
                data["sender_display_name"] = None
            else:
                message.notification.title = "New message"

        content_obj = data.get("content")

        relates_to = content_obj.get("m.relates_to")
        rel_type = relates_to.get("rel_type") if relates_to else None
        is_replaced = rel_type == "m.replace"
        # isEncrypted = data.get("m.room.encrypted")

        if data.get("type"):
            message.data["msg_type"] = data.get("type")
        else:
            message.data["msg_type"] = "Test"

        if data.get("call_id"):
            message.data["call_id"] = data.get("call_id")

        if content_obj and content_obj.get("call_id"):
            message.data["call_id"] = content_obj.get("call_id")

        if data.get("type") and data.get("type") == "m.call.invite":
            message.notification.body = "Incoming call"
        elif content_obj and content_obj.get("msgtype") == "m.image":
            if data.get("sender_display_name"):
                message.notification.body += " sent picture"
            else:
                message.notification.body = "New picture"
        elif content_obj and content_obj.get("msgtype") == "m.text":
            textContent = re.sub(r"@\w+:(\w+)", GcmPushkin._regex_sender_name, content_obj.get("body"))

            if data.get("sender_display_name"):
                message.notification.body += f': {textContent}'
            else:
                message.notification.body = textContent

            if is_replaced:
                message.notification.body += ' (edited)'
        elif content_obj and content_obj.get("msgtype") == "m.file":
            if data.get("sender_display_name"):
                message.notification.body += f' 📎 {content_obj.get("pbody").get("name")}'
            else:
                message.notification.body = f'📎 {content_obj.get("pbody").get("name")}'
        elif content_obj and content_obj.get("msgtype") == "m.audio":
            if data.get("sender_display_name"):
                message.notification.body += " sent new voice message"
            else:
                message.notification.body += " sent you new voice message"
        else:
            if data.get("sender_display_name"):
                if (is_replaced):
                    message.notification.body += f" edited the message"
                elif data.get("room_name") and data.get("room_name").startswith("@"):
                    message.notification.body += f" sent new encrypted message"
                else:
                    message.notification.body += f" sent you new encrypted message"
            else:
                if (is_replaced):
                    message.notification.body = "User edited the message"
                else:
                    message.notification.body = "New encrypted message"

        return message

    @staticmethod
    def _regex_sender_name(match_obj):
        if match_obj.group(1) is not None:
            return '@' + match_obj.group(1)


class CanonicalRegIdStore(object):
    TABLE_CREATE_QUERY = """
        CREATE TABLE IF NOT EXISTS gcm_canonical_reg_id (
            reg_id TEXT PRIMARY KEY,
            canonical_reg_id TEXT NOT NULL
        );
        """

    def __init__(self, db: ConnectionPool, engine: str):
        """
        Args:
            db (adbapi.ConnectionPool): database to prepare
            engine (str):
                Database engine to use. Shoud be either "sqlite" or "postgresql".
        """
        self.db = db
        self.engine = engine

    async def setup(self):
        """
        Prepares, if necessary, the database for storing canonical registration IDs.

        Separate method from the constructor because we wait for an async request
        to complete, so it must be an `async def` method.
        """
        await self.db.runOperation(self.TABLE_CREATE_QUERY)

    async def set_canonical_id(self, reg_id, canonical_reg_id):
        """
        Associates a GCM registration ID with a canonical registration ID.
        Args:
            reg_id (str): a registration ID
            canonical_reg_id (str): the canonical registration ID for `reg_id`
        """
        if self.engine == "sqlite":
            await self.db.runOperation(
                "INSERT OR REPLACE INTO gcm_canonical_reg_id VALUES (?, ?);",
                (reg_id, canonical_reg_id),
            )
        else:
            await self.db.runOperation(
                """
                INSERT INTO gcm_canonical_reg_id VALUES (%s, %s)
                ON CONFLICT (reg_id) DO UPDATE
                    SET canonical_reg_id = EXCLUDED.canonical_reg_id;
                """,
                (reg_id, canonical_reg_id),
            )

    async def get_canonical_ids(self, reg_ids):
        """
        Retrieves the canonical registration ID for multiple registration IDs.

        Args:
            reg_ids (iterable): registration IDs to retrieve canonical registration
                IDs for.

        Returns (dict):
            mapping of registration ID to either its canonical registration ID,
            or `None` if there is no entry.
        """
        parameter_key = "?" if self.engine == "sqlite" else "%s"
        rows = dict(
            await self.db.runQuery(
                """
                SELECT reg_id, canonical_reg_id
                FROM gcm_canonical_reg_id
                WHERE reg_id IN (%s)
                """
                % (",".join(parameter_key for _ in reg_ids)),
                reg_ids,
            )
        )
        return {reg_id: dict(rows).get(reg_id) for reg_id in reg_ids}
