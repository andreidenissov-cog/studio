# -*- coding: utf-8 -*-
import time
import threading

import pika

from studio.storage.storage_setup import get_storage_verbose_level
from studio.util import logs

class RMQueue:
    """This publisher will handle failures and closures and will
    attempt to restart things

    delivery confirmations are used to track messages that have
    been sent and if they have been confirmed

    """

    # pylint: disable=too-many-instance-attributes

    # Doing it properly will require more serious refactoring
    # then we want to do just now.
    def __init__(self, queue, route, amqp_url='',
                 config=None, logger=None):
        """Setup the example publisher object, passing in the URL we will use
        to connect to RabbitMQ.
        """
        self._rmq_lock = threading.RLock()
        self._connection = None
        self._channel = None
        self._consumer = None
        self._consume_ready = False

        self._msg_tracking_lock = threading.RLock()
        self._deliveries = []
        self._acked = 0
        self._nacked = 0
        self._message_number = 0

        self._rmq_msg = None
        self._rmq_id = None

        self._stopping = False
        self._exchange = 'StudioML.topic'
        self._exchange_type = 'topic'
        self._routing_key = route

        self._url = amqp_url
        self._is_persistent: bool = False

        if logger is not None:
            self._logger = logger
        else:
            self._logger = logs.get_logger('RabbitMQ')
            self._logger.setLevel(get_storage_verbose_level())

        if config is not None:
            # extract from the config data structure any settings related to
            # queue messaging for rabbit MQ
            if 'cloud' in config:
                if 'queue' in config['cloud']:
                    if 'rmq' in config['cloud']['queue']:
                        self._url = config['cloud']['queue']['rmq']
                        self._logger.warning('use queue url %s', self._url)
                        flag_persistent = config['cloud']['queue']\
                            .get('persistent', False)
                        if isinstance(flag_persistent, str):
                            flag_persistent = flag_persistent.lower() == 'true'
                        self._is_persistent = flag_persistent

        self._queue = queue
        self._queue_deleted = True
        self._connection_failed = False
        self._connection_failure_reason = None

        # The pika library for RabbitMQ has an asynchronous run method
        # that needs to run forever and will do reconnections etc
        # automatically for us
        thr = threading.Thread(target=self._run, args=(), kwargs={})
        thr.setDaemon(True)
        thr.start()
        self._wait_queue_created(600)

    def _channel_is_valid(self):
        return self._channel is not None and \
               self._channel.is_open

    def _connection_is_valid(self):
        return self._connection is not None and \
               not self._connection.is_closed

    def connect(self):
        """
        When the connection is established, the _on_connection_open method
        will be invoked by pika. If you want the reconnection to work, make
        sure you set stop_ioloop_on_close to False, which is not the default
        behavior of this adapter.

        :rtype: pika.SelectConnection

        """
        params = pika.URLParameters(self._url)
        new_connection = pika.SelectConnection(
            params,
            on_open_callback=self._on_connection_open,
            on_open_error_callback=self._on_connection_error,
            on_close_callback=self._on_connection_closed,
            custom_ioloop=None,
            internal_connection_workflow=True)
        return new_connection

    def _wait_queue_deleted(self, timeout_in_secs):
        """
        Polling wait till underlying RMQ queue
        is confirmed deleted or
        specified timeout (in seconds) is reached.
        """
        for _ in range(timeout_in_secs):
            if self._queue_deleted:
                self._logger.info('Queue %s is confirmed deleted', self._queue)
                return
            time.sleep(1)
        self._logger.info('Timeout %s seconds reached while waiting for queue %s deletion.',
                          timeout_in_secs, self._queue)
        return

    def _wait_queue_created(self, timeout_in_secs):
        """
        Polling wait till underlying   queue
        is confirmed created.
        If specified timeout (in seconds) is reached,
        exception is raised.
        """
        self._logger.info('Waiting for queue %s to be created.', self._queue)
        for _ in range(timeout_in_secs):
            if self._connection_failed:
                err_message = "FAILED to connect queue {0} Reason: {1}"\
                    .format(self._queue, self._connection_failure_reason)
                self._logger.error(err_message)
                raise ValueError(err_message)

            if not self._queue_deleted:
                self._logger.info('Queue %s is confirmed created and bound.',
                                  self._queue)
                return
            time.sleep(1)
        err_message = 'Timeout {0} seconds reached while waiting for queue {1} creation.'\
            .format(timeout_in_secs, self._queue)
        self._logger.error(err_message)
        raise ValueError(err_message)

    def _on_delete_ok(self, unused_frame):
        """
        This method is invoked by pika when it receives the Queue.DeleteOk
        response from RabbitMQ.
        """

        self._logger.info("Queue %s deleted OK.", self._queue)
        self._queue_deleted = True

    def _delete_queue_attempt(self):
        """
        Try to delete underlying RMQ queue,
        which will also unbind and purge it.
        """
        if self._queue_deleted:
            self._logger.info("Queue %s is already deleted.", self._queue)
            return
        with self._rmq_lock:
            if not self._channel_is_valid():
                self._logger.info(
                    "Channel to queue %s is None or closed: cannot delete queue.",
                                  self._queue)
                self._queue_deleted = True
                return
            self._channel.queue_delete(self._queue, callback=self._on_delete_ok)

        self._wait_queue_deleted(30)

    def _delete_queue(self):
        """
        Delete underlying RMQ queue,
        which will also unbind and purge it.
        Retry operation if necessary for fixed number of times.
        """
        num_retries = 5
        retries_cnt = num_retries
        go_on = True
        while retries_cnt > 0 and go_on:
            self._logger.info("Trying to delete queue %s retries left: %s",
                              self._queue, retries_cnt)
            self._delete_queue_attempt()
            go_on = not self._queue_deleted
            retries_cnt -= 1

        if not self._queue_deleted:
            self._logger.info("FAILED to delete queue %s after %s retries. IGNORING.",
                              self._queue, num_retries)

    def _on_connection_open(self, unused_connection):
        """
        :type unused_connection: pika.SelectConnection
        """
        _ = unused_connection

        self._logger.info("Connection is opened")
        self.open_channel()

    def _on_connection_error(self, unused_connection, reason):
        self._connection_failure_reason = repr(reason)
        self._connection_failed = True

    def _on_connection_closed(self, connection, reason):
        """
        on any close reconnect to RabbitMQ, until the stopping is set

        :param pika.connection.Connection connection: The closed connection obj
        :param Exception reason: why the connection was closed

        """
        _ = connection
        self._logger.info('connection to queue %s closed. Reason: %s',
                          self._queue, repr(reason))
        with self._rmq_lock:
            if self._stopping:
                self._connection.ioloop.stop()
            else:
                retry_timeout = 3
                # retry in retry_timeout seconds
                self._logger.info('connection closed, retry in %s seconds: %s',
                                  retry_timeout, repr(reason))
                self._connection.ioloop.call_later(retry_timeout,
                                                   self._reconnect)

    def _reconnect(self):
        """Will be invoked by the IOLoop timer if the connection is
        closed. See the _on_connection_closed method.

        """
        if not self._stopping:
            with self._rmq_lock:
                # Create a new connection
                self._connection = self.connect()

    def open_channel(self):
        """
        open a new channel using the Channel.Open RPC command. RMQ confirms
        the channel is open by sending the Channel.OpenOK RPC reply, the
        _on_channel_open method will be invoked.
        """
        self._logger.info('creating a new channel')

        with self._rmq_lock:
            self._connection.channel(on_open_callback=self._on_channel_open)

    def _on_channel_open(self, channel):
        """
        on channel open, declare the exchange to use

        :param pika.channel.Channel channel: The channel object

        """
        self._logger.info('created a new channel')

        with self._rmq_lock:
            self._channel = channel
            self._channel.basic_qos(prefetch_count=0)
            self._channel.add_on_close_callback(self._on_channel_closed)

        self.setup_exchange(self._exchange)

    def _on_channel_closed(self, channel, reason):
        """
        physical network issues and logical protocol abuses can
        result in a closure of the channel.

        :param pika.channel.Channel channel: The closed channel
        :param int reply_code: The numeric reason the channel was closed
        :param Exception reason: why the channel was closed

        """
        _ = channel
        self._logger.info('channel closed %s', repr(reason))
        with self._rmq_lock:
            if not self._stopping and \
                    self._connection_is_valid():
                self._connection.close()

    def setup_exchange(self, exchange_name):
        """
        exchange setup by invoking the Exchange.Declare RPC command.
        When complete, the _on_exchange_declareok method will be invoked
        by pika.

        :param str|unicode exchange_name: The name of the exchange to declare

        """
        self._logger.debug('declaring exchange %s', exchange_name)
        with self._rmq_lock:
            self._channel.exchange_declare(exchange_name,
                                           callback=self._on_exchange_declareok,
                                           exchange_type=self._exchange_type,
                                           durable=True,
                                           auto_delete=True)

    def _on_exchange_declareok(self, unused_frame):
        """
        completion callback for the Exchange.Declare RPC command.

        :param pika.Frame.Method unused_frame: Exchange.DeclareOk response

        """
        self._logger.debug('declared exchange %s', self._exchange)
        self.setup_queue(self._queue)

    def setup_queue(self, queue_name):
        """
        Setup the queue invoking the Queue.Declare RPC command.
        The completion callback is, the _on_queue_declareok method.

        :param str|unicode queue_name: The name of the queue to declare.

        """
        self._logger.debug('declare queue %s', queue_name)
        with self._rmq_lock:
            self._channel.queue_declare(queue_name,
                                        durable=self._is_persistent,
                                        callback=self._on_queue_declareok)

    def _on_queue_declareok(self, method_frame):
        """
        Queue.Declare RPC completion callback.
        In this method the queue and exchange are bound together
        with the routing key by issuing the Queue.Bind
        RPC command.

        The completion callback is the _on_bindok method.

        :param pika.frame.Method method_frame: The Queue.DeclareOk frame

        """
        _ = method_frame
        self._logger.info('binding %s to queue %s with %s',
                          self._exchange, self._queue, self._routing_key)
        with self._rmq_lock:
            self._channel.queue_bind(self._queue,
                                     self._exchange,
                                     routing_key=self._routing_key,
                                     callback=self._on_bindok)

    def _on_bindok(self, unused_frame):
        """This method is invoked by pika when it receives the Queue.BindOk
        response from RabbitMQ. Since we know we're now setup and bound, it's
        time to start publishing."""
        self._logger.info('bound %s to queue %s with %s',
                          self._exchange, self._queue, self._routing_key)
        # Now underlying RMQ queue is setup: set the state flag
        self._queue_deleted = False

        # Send the Confirm.Select RPC method to RMQ to enable delivery
        # confirmations on the channel. The only way to turn this off is to close
        # the channel and create a new one.
        #
        # When the message is confirmed from RMQ, the
        # _on_delivery_confirmation method will be invoked passing in a Basic.Ack
        # or Basic.Nack method from RMQ that will indicate which messages it
        # is confirming or rejecting.

        with self._rmq_lock:
            self._channel.confirm_delivery(self._on_delivery_confirmation)

    def _on_delivery_confirmation(self, method_frame):
        """
        RMQ callback for responses to a Basic.Publish RPC
        command, passing in either a Basic.Ack or Basic.Nack frame with
        the delivery tag of the message that was published. The delivery tag
        is an integer counter indicating the message number that was sent
        on the channel via Basic.Publish. Here we're just doing house keeping
        to keep track of stats and remove message numbers that we expect
        a delivery confirmation of from the list used to keep track of messages
        that are pending confirmation.

        :param pika.frame.Method method_frame: Basic.Ack or Basic.Nack frame

        """
        confirmation_type = method_frame.method.NAME.split('.')[1].lower()
        self._logger.debug('received %s for delivery tag: %s',
                           confirmation_type,
                           str(method_frame.method.delivery_tag))

        with self._msg_tracking_lock:
            if confirmation_type == 'ack':
                self._acked += 1
            elif confirmation_type == 'nack':
                self._nacked += 1
            self._deliveries.remove(method_frame.method.delivery_tag)
            self._logger.info(
                'published %s messages, %s have yet to be confirmed,'
                ' %s were acked and %s were nacked',
                str(self._message_number),
                str(len(self._deliveries)),
                str(self._acked), str(self._nacked))

    def _run(self):
        """
        Blocking run loop, connecting and then starting the IOLoop.
        """
        self._logger.info('RMQ started')
        with self._msg_tracking_lock:
            self._deliveries = []
            self._acked = 0
            self._nacked = 0
            self._message_number = 0
        while not self._stopping:
            self._connection = None

            try:
                with self._rmq_lock:
                    self._connection = self.connect()
                self._logger.info('RMQ connected')
                self._connection.ioloop.start()
            except KeyboardInterrupt:
                self.stop()
                if self._connection_is_valid():
                    # Finish closing
                    self._connection.ioloop.start()

        self._logger.info('RMQ stopped')

    def stop(self):
        """
        Stop the by closing the channel and connection and setting
        a stop state.

        The IOLoop is started independently which means we need this
        method to handle things such as the Try/Catch when KeyboardInterrupts
        are caught.
        Starting the IOLoop again will allow the publisher to cleanly
        disconnect from RMQ.
        """
        self._logger.info('stopping')
        self._stopping = True
        self._close_connection()

    def close_channel(self):
        """
        Close channel by sending the Channel.Close RPC command.
        """
        with self._rmq_lock:
            if self._channel_is_valid():
                self._logger.info('closing the channel')
                self._channel.close()

    def _close_connection(self):
        with self._rmq_lock:
            if self._connection_is_valid():
                self._logger.info('closing connection')
                self._connection.close()

    def clean(self, timeout=0):
        while True:
            msg = self.dequeue(timeout=timeout)
            if msg is None:
                return

    def get_name(self):
        return self._queue

    def enqueue(self, msg, retries=10):
        """
        Publish a message to RMQ, appending a list of deliveries with
        the message number that was sent.  This list will be used to
        check for delivery confirmations in the
        on_delivery_confirmations method.
        """
        if self._url is None:
            raise Exception('url for rmq not initialized')

        if msg is None:
            raise Exception(
                'message was None, it needs a meaningful value to be sent')

        # Wait to see if the channel gets opened
        tries = retries
        while tries != 0:
            if self._channel is None:
                self._logger.warning(
                    'failed to send message (%s tries left) to %s as '
                    'the channel API was not initialized',
                    tries, self._url)
            elif not self._channel.is_open:
                self._logger.warning(
                    'failed to send message (%s tries left) to %s as '
                    'the channel was not open',
                    tries, self._url)
            elif self._queue_deleted:
                self._logger.warning(
                    'failed to send message (%s tries left) to %s as '
                    'the queue is not yet created',
                    tries, self._url)
            else:
                break

            time.sleep(1)
            tries -= 1

        if tries == 0:
            raise Exception('studioml request could not be sent')

        self._logger.debug('sending message %s to %s ', msg, self._url)
        if self._is_persistent:
            properties = pika.BasicProperties(
                app_id='studioml',
                delivery_mode=2,  # make message persistent
                content_type='application/json')
        else:
            properties = pika.BasicProperties(
                app_id='studioml',
                content_type='application/json')

        self._channel.basic_publish(exchange=self._exchange,
                                    routing_key=self._routing_key,
                                    body=msg,
                                    properties=properties,
                                    mandatory=True)
        self._logger.debug('sent message to %s ', self._url)

        with self._msg_tracking_lock:
            self._message_number += 1

            message_number = self._message_number
            self._deliveries.append(self._message_number)

        tries = retries
        while tries != 0:
            time.sleep(1)

            with self._msg_tracking_lock:
                if message_number not in self._deliveries:
                    self._logger.info(
                        'sent message acknowledged to %s after waiting %s seconds',
                        self._url, retries-tries+1)

                    return message_number
                tries -= 1

        msg: str = ('studioml message {0} was never acknowledged to {1} ' +
                    'after waiting {2} seconds')\
            .format(message_number, self._url, retries)
        raise Exception(msg)

    def dequeue(self, acknowledge=True, timeout=0):
        # start the consumer and allow single messages to returned via
        # this method to the caller blocking using a callback lock
        # while waiting
        for i in range(timeout + 1):
            with self._rmq_lock:
                if self._consumer is None and self._channel is not None:
                    self._consumer = \
                        self._channel.basic_consume(self._queue, self._on_message)

                if self._rmq_msg is not None:
                    self._logger.info('message %s from %s ',
                                      self._rmq_msg, self._url)
                    rec_msg = self._rmq_msg
                    rec_id = self._rmq_id
                    if acknowledge:
                        self.acknowledge(self._rmq_id)
                    return rec_msg, rec_id
                self._logger.info('idle %s %s', self._url, self._queue)

            if i >= timeout:
                self._logger.info('timed-out trying to dequeue from %s',
                                  self._url)
                return None, None

            time.sleep(1)

        self._logger.debug('dequeue done from %s', self._url)
        return None, None

    def _on_message(self, unused_channel, basic_deliver, properties, body):

        _ = unused_channel
        _ = properties

        with self._rmq_lock:
            if self._channel is not None:
                # Cancel the consumer as we only consume 1 message
                # at a time
                self._channel.basic_cancel(consumer_tag=self._consumer)
            self._consumer = None

            # If we already had a delivered message, reject the one we just got
            if self._rmq_msg is not None:
                if self._connection is not None:
                    self._channel.basic_nack(
                        delivery_tag=basic_deliver.delivery_tag)
            else:
                self._rmq_msg = body
                self._rmq_id = basic_deliver.delivery_tag

    def has_next(self):
        raise NotImplementedError(
            'using has_next with distributed queue is not supportable')

    def acknowledge(self, ack_id):
        with self._rmq_lock:
            self._rmq_msg = None
            self._rmq_id = None
            if self._channel is None:
                return None
            result = self._channel.basic_ack(delivery_tag=ack_id)
            return result

    def hold(self, ack_id, minutes):
        # Nothing is needed here as the message will remain while the channel
        # remains open, or we nack it
        pass

    def shutdown(self, delete_queue=True):
        """
        Delete current RabbitMQ in use.
        This involves delete for the queue
        and subsequent closing of our connection.
        """
        if delete_queue:
            self._logger.info("Deleting RMQ %s", str(self._queue))
            self._delete_queue()
        self._logger.info("Closing RMQ connection for %s", str(self._queue))
        self.stop()
