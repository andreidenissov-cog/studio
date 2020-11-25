import calendar

import os
from urllib.parse import urlparse
import boto3
import botocore

from . import logs
from . import util
from .credentials import Credentials
from .storage_handler import StorageHandler
from .storage_type import StorageType
from .model_setup import get_model_verbose_level

class S3StorageHandler(StorageHandler):
    def __init__(self, config,
                 measure_timestamp_diff=False,
                 compression=None):
        self.logger = logs.getLogger(self.__class__.__name__)
        self.logger.setLevel(get_model_verbose_level())
        self.credentials: Credentials =\
            Credentials.getCredentials(config)
        self.client = boto3.client(
            's3',
            aws_access_key_id=self.credentials.get_key(),
            aws_secret_access_key=self.credentials.get_secret_key(),
            endpoint_url=config.get('endpoint'),
            region_name=config.get('region'))

        if compression is None:
            compression = config.get('compression')

        self.endpoint = self.client._endpoint.host

        self.bucket = config['bucket']
        try:
            buckets = self.client.list_buckets()
        except Exception as exc:
            msg: str = "FAILED to list buckets for {0}: {1}"\
                .format(self.endpoint, exc)
            self._report_fatal(msg)

        if self.bucket not in [b['Name'] for b in buckets['Buckets']]:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as exc:
                msg: str = "FAILED to create bucket {0} for {1}: {2}"\
                    .format(self.bucket, self.endpoint, exc)
                self._report_fatal(msg)

        super().__init__(StorageType.storageS3,
            self.logger,
            measure_timestamp_diff,
            compression=compression)

        #self.set_storage_client(self.client)

    def _not_found(self, response):
        try:
            return response['Error']['Code'] == '404'
        except Exception:
            return False

    def upload_file(self, key, local_path):
        if not os.path.exists(local_path):
            self.logger.info(
                "Local path {0} does not exist. SKIPPING upload to {1}/{2}"
                    .format(local_path, self.bucket, key))
            return False
        try:
            self.client.upload_file(local_path, self.bucket, key)
            return True
        except Exception as exc:
            self._report_fatal("FAILED to upload file {0} to {1}/{2}: {3}"
                               .format(local_path, self.bucket, key, exc))
            return False

    def download_file(self, key, local_path):
        try:
            self.client.download_file(self.bucket, key, local_path)
            return True
        except botocore.exceptions.ClientError as exc:
            if self._not_found(exc.response):
                self.logger.info(
                    "No key found: {0}/{1}. SKIPPING download to {2}"
                    .format(self.bucket, key, local_path))
            else:
                self._report_fatal("FAILED to download file {0} from {1}/{2}: {3}"
                                   .format(local_path, self.bucket, key, exc))
            return False
        except Exception as exc:
            self._report_fatal("FAILED to download file {0} from {1}/{2}: {3}"
                               .format(local_path, self.bucket, key, exc))
            return False

    def delete_file(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def get_file_url(self, key, method='GET'):
        if method == 'GET':
            return self.client.generate_presigned_url(
                'get_object', Params={'Bucket': self.bucket, 'Key': key})
        elif method == 'PUT':
            return self.client.generate_presigned_url(
                'put_object', Params={'Bucket': self.bucket, 'Key': key})
        else:
            raise ValueError('Unknown method {0} in get_file_url()'.format(method))

    def get_file_timestamp(self, key):
        time_updated = False
        try:
            obj = self.client.head_object(Bucket=self.bucket, Key=key)
            time_updated = obj.get('LastModified', None)
        except botocore.exceptions.ClientError as exc:
            if self._not_found(exc.response):
                self.logger.info(
                    "No key found: {0}/{1}. Cannot get timestamp."
                        .format(self.bucket, key))
            else:
                self.logger.error("FAILED to get timestamp for S3 object {0}/{1}"
                        .format(self.bucket, key))
            return None
        except BaseException:
            self.logger.error("FAILED to get timestamp for S3 object {0}/{1}"
                              .format(self.bucket, key))
            return None

        if time_updated:
            timestamp = calendar.timegm(time_updated.timetuple())
            return timestamp
        else:
            return None

    def get_qualified_location(self, key):
        url = urlparse(self.endpoint)
        location: str = 's3://' + url.netloc + '/' + self.bucket + '/' + key

        return location

    def get_bucket(self):
        return self.bucket

    def get_client(self):
        return self.client

    def _report_fatal(self, msg: str):
        util.report_fatal(msg, self.logger)
