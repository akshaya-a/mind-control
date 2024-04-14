from functools import lru_cache, partial
import logging
import os

# Eventing - move this to plugin
from contextlib import asynccontextmanager
import asyncio

# Core functionality
from fastapi import FastAPI

import collections


from .mlmodels import log_system_models
from .mqtt import setup_mqtt_client, listen_to_mqtt
from .mlflow_bridge import connect_to_mlflow, poll_registry
from .db.setup import setup_db, insert_summary
from .config import AppSettings
from .routers import deployed_models, info, ui
from .const import ROUTE_PREFIX


_logger = logging.getLogger(__name__)


def write_healthcheck_file(settings: AppSettings):
    # Write readiness: https://skarnet.org/software/s6/notifywhenup.html
    notification_fd = settings.notify_fd
    if notification_fd:
        os.write(int(notification_fd), b"\n")
        os.close(int(notification_fd))


@lru_cache
def get_settings():
    # env vars can populate the settings
    return AppSettings()  # pyright: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_settings = get_settings()
    _logger.info("Starting mindctrl server with settings:")
    _logger.info(app_settings.model_dump())

    asyncio.create_task(poll_registry(10.0))

    # The buffer should be enhanced to be token-aware
    state_ring_buffer: collections.deque[dict] = collections.deque(maxlen=20)
    _logger.info("Setting up DB")
    # TODO: convert to ABC with a common interface
    if not app_settings.store.store_type == "psql":
        raise ValueError(f"unknown store type: {app_settings.store.store_type}")
    engine = await setup_db(app_settings.store)
    insert_summary_partial = partial(
        insert_summary, engine, app_settings.include_challenger_models
    )

    _logger.info("Setting up MQTT")
    if not app_settings.events.events_type == "mqtt":
        raise ValueError(f"unknown events type: {app_settings.events.events_type}")

    mqtt_client = setup_mqtt_client(app_settings.events)
    loop = asyncio.get_event_loop()
    _logger.info("Starting MQTT listener")
    mqtt_listener_task = loop.create_task(
        listen_to_mqtt(mqtt_client, state_ring_buffer, insert_summary_partial)
    )

    _logger.info("Logging models")
    loaded_models = log_system_models(app_settings.force_publish_models)
    connect_to_mlflow(app_settings)

    write_healthcheck_file(app_settings)

    _logger.info("Finished server setup")
    # Make resources available to requests via .state
    yield {
        "state_ring_buffer": state_ring_buffer,
        "loaded_models": loaded_models,
        "database_engine": engine,
    }

    # Cancel the task
    mqtt_listener_task.cancel()
    # Wait for the task to be cancelled
    try:
        await mqtt_listener_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(deployed_models.router, prefix=ROUTE_PREFIX)
app.include_router(info.router, prefix=ROUTE_PREFIX)
app.include_router(ui.router, prefix=ROUTE_PREFIX)
