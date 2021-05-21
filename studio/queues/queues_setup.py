"""Data providers."""
import uuid

from studio.queues.local_queue import LocalQueue
from studio.queues.sqs_queue import SQSQueue
from studio.queues.qclient_cache import get_cached_queue, shutdown_cached_queue
from studio.util.util import parse_verbosity

from studio.db_providers import db_provider_setup
from studio.storage.storage_setup import setup_storage, get_storage_db_provider,\
    reset_storage, set_storage_verbose_level
from studio.util import logs

def get_queue(
        queue_name=None,
        cloud=None,
        config=None,
        logger=None,
        close_after=None,
        verbose=10):
    if queue_name is None:
        if cloud in ['gcloud', 'gcspot']:
            queue_name = 'pubsub_' + str(uuid.uuid4())
        elif cloud in ['ec2', 'ec2spot']:
            queue_name = 'sqs_' + str(uuid.uuid4())
        else:
            queue_name = 'local'

    if queue_name.startswith('ec2') or \
       queue_name.startswith('sqs'):
        return SQSQueue(queue_name, config=config, logger=logger)
    elif queue_name.startswith('rmq_'):
        return get_cached_queue(
            name=queue_name,
            route='StudioML.' + queue_name,
            config=config,
            close_after=close_after,
            logger=logger,
            verbose=verbose)
    elif queue_name == 'local':
        return LocalQueue(verbose=verbose)
    else:
        return None

def shutdown_queue(queue, logger=None, delete_queue=True):
    if queue is None:
        return
    queue_name = queue.get_name()
    if queue_name.startswith("rmq_"):
        shutdown_cached_queue(queue, logger, delete_queue)
    else:
       queue.shutdown(delete_queue)
