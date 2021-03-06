#!/usr/bin/env python3

import os
import sys
import time
import json
import logging
import requests
import boto3
import kubernetes
from dateutil.tz import tzlocal

# Workaround for following issues
# https://github.com/kubernetes-client/python/issues/376
# https://github.com/kubernetes-client/python-base/issues/57

from kubernetes.client.models.v1_object_reference import V1ObjectReference
from kubernetes.client.models.v1_event import V1Event


def set_involved_object(self, involved_object):
    if involved_object is None:
        involved_object = V1ObjectReference()
    self._involved_object = involved_object


setattr(V1Event, 'involved_object', property(
    fget=V1Event.involved_object.fget, fset=set_involved_object))


import pydoc
from kubernetes.watch import Watch

PYDOC_RETURN_LABEL = ":return:"


class SimpleNamespace:

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _find_return_type(func):
    for line in pydoc.getdoc(func).splitlines():
        if line.startswith(PYDOC_RETURN_LABEL):
            return line[len(PYDOC_RETURN_LABEL):].strip()
    return ""


def iter_resp_lines(resp):
    prev = ""
    for seg in resp.read_chunked(decode_content=False):
        if isinstance(seg, bytes):
            seg = seg.decode('utf8')
        seg = prev + seg
        lines = seg.split("\n")
        if not seg.endswith("\n"):
            prev = lines[-1]
            lines = lines[:-1]
        else:
            prev = ""
        for line in lines:
            if line:
                yield line


def my_unmarshal_event(self, data, return_type):
    js = json.loads(data)
    logger.debug("func: my_unmarshal_event")
    logger.debug(str(js))
    js['raw_object'] = js['object']
    if return_type:
        if js.get('type') == 'ERROR':
            if js.get('object', {}).get('reason') == 'Expired':
                raise TimeoutError(js['object']['message'])
        obj = SimpleNamespace(data=json.dumps(js['raw_object']))
        js['object'] = self._api_client.deserialize(obj, return_type)
        if hasattr(js['object'], 'metadata'):
            self.resource_version = js['object'].metadata.resource_version
    return js


Watch.unmarshal_event = my_unmarshal_event

# End of workaround


logger = logging.getLogger()


def read_env_variable_or_die(env_var_name):
    value = os.environ.get(env_var_name, '')
    if value == '':
        message = 'Env variable {} is not defined or set to empty string. Set it to non-empty string and try again'.format(
            env_var_name)
        logger.error(message)
        raise EnvironmentError(message)
    return value


def post_slack_message(hook_url, message):
    logger.debug(
        'Posting the following message to {}:\n{}'.format(hook_url, message))
    headers = {'Content-type': 'application/json'}
    requests.post(hook_url, data=str(message), headers=headers)


def is_type_in_skip_list(event, skip_list):
    return True if event['type'] in skip_list else False


def is_reason_in_include_list(event, include_list):
    return True if event['object'].reason in include_list else False


def format_k8s_event_to_slack_message(event_object, cluster_name, notify=''):
    event = event_object['object']
    message = {
        'attachments': [{
            'color': '#36a64f',
            'title': '[{}] {}'.format(cluster_name, event.message),
            'text': 'event type: {}, event reason: {}'.format(event_object['type'], event.reason),
            'footer': 'First time seen: {}, Last time seen: {}, Count: {}'.format(event.first_timestamp.strftime('%d/%m/%Y %H:%M:%S %Z'),
                                                                                  event.last_timestamp.strftime(
                                                                                      '%d/%m/%Y %H:%M:%S %Z'),
                                                                                  event.count),
            'fields': [
                {
                    'title': 'Involved object',
                    'value': 'kind: {}, name: {}, namespace: {}'.format(event.involved_object.kind,
                                                                        event.involved_object.name,
                                                                        event.involved_object.namespace),
                    'short': 'true'
                },
                {
                    'title': 'Metadata',
                    'value': 'name: {}, creation time: {}'.format(event.metadata.name,
                                                                  event.metadata.creation_timestamp.strftime('%d/%m/%Y %H:%M:%S %Z')),
                    'short': 'true'
                }
            ],
        }]
    }
    if event.type == 'Warning':
        message['attachments'][0]['color'] = '#f1c232'
        if notify != '':
            message['text'] = '{} there is a warning for you to check'.format(
                notify)

    return json.dumps(message)


def post_cw_log(event, cw_log_group, client_cw_logs):
    pod = event['object'].involved_object.name
    kind = event['object'].involved_object.kind
    namespace = event['object'].involved_object.namespace
    creation_epoch_ms = int(
        event['object'].metadata.creation_timestamp.timestamp()) * 1000
    cw_log_stream = '/{}/{}/{}'.format(
        namespace, kind, pod)

    r = client_cw_logs.describe_log_streams(
        logGroupName=cw_log_group, logStreamNamePrefix=cw_log_stream, limit=50)

    kwargs = {'logGroupName': cw_log_group,
              'logStreamName': cw_log_stream,
              'logEvents': [
                  {
                      'timestamp': creation_epoch_ms,
                      'message': str(event)
                  }
              ]}

    logger.debug(str(r))
    if not r['logStreams']:  # New log stream
        logger.info('Create cloudwatch log stream {} in log group {}'.format(
            cw_log_stream, cw_log_group))
        client_cw_logs.create_log_stream(
            logGroupName=cw_log_group, logStreamName=cw_log_stream)
        client_cw_logs.put_log_events(**kwargs)
    else:
        for ls in r['logStreams']:
            if ls['logStreamName'] == cw_log_stream:
                if 'uploadSequenceToken' in r['logStreams'][0]:
                    kwargs['sequenceToken'] = r['logStreams'][0]['uploadSequenceToken']
                client_cw_logs.put_log_events(**kwargs)
                break


def main():

    if os.environ.get('K8S_EVENTS_STREAMER_DEBUG', False):
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG,
                            format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    else:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                            format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    logger.info("Reading configuration...")
    k8s_cluster_name = read_env_variable_or_die(
        'K8S_EVENTS_STREAMER_CLUSTER_NAME')
    aws_region = os.environ.get('K8S_EVENTS_STREAMER_AWS_REGION', 'us-east-1')
    types_to_skip = os.environ.get(
        'K8S_EVENTS_STREAMER_SKIP_EVENT_TYPES', 'DELETE').split()
    reasons_to_include = os.environ.get(
        'K8S_EVENTS_STREAMER_LIST_OF_REASONS_TO_INCLUDE', '').split()

    # CloudWatch Logs
    cw_log_group = os.environ.get('K8S_EVENTS_STREAMER_CW_LOG_GROUP', '')

    # Slack
    users_to_notify = os.environ.get('K8S_EVENTS_STREAMER_USERS_TO_NOTIFY', '')
    slack_web_hook_url = os.environ.get(
        'K8S_EVENTS_STREAMER_INCOMING_WEB_HOOK_URL', '')

    kubernetes.config.load_incluster_config()
    v1 = kubernetes.client.CoreV1Api()
    k8s_watch = kubernetes.watch.Watch()
    logger.info("Configuration is OK")

    cached_event_uids = []
    if cw_log_group:
        client_cw_logs = boto3.client('logs', region_name=aws_region)
    while True:
        logger.info("Processing events for 2 hours...")
        try:
            for event in k8s_watch.stream(v1.list_event_for_all_namespaces, timeout_seconds=7200):
                logger.debug(str(event))
                if not event['object'].involved_object:
                    logger.info(
                        'Found empty involved_object in the event. Skip this one.'
                    )
                    continue
                if is_type_in_skip_list(event, types_to_skip) == True:
                    logger.debug(
                        'Event type {} is in the skip list. Skip this one.'.format(event['type']))
                    continue
                if is_reason_in_include_list(event, reasons_to_include) == False:
                    logger.debug(
                        'Event reason {} is not in the include list. Skip this one.'.format(event['object'].reason))
                    continue

                event_uid = event['object'].metadata.uid
                if not event_uid in cached_event_uids:
                    if cw_log_group:
                        post_cw_log(event, cw_log_group, client_cw_logs)
                    if slack_web_hook_url:
                        message = format_k8s_event_to_slack_message(
                            event, k8s_cluster_name, users_to_notify)
                        post_slack_message(slack_web_hook_url, message)
                    cached_event_uids.append(event_uid)
                    logger.debug(
                        'Cached event uids: {}'.format(cached_event_uids))
        except TimeoutError as e:
            logger.error(e)
            logger.warning('Wait 30 sec and check again due to error.')
            time.sleep(30)
            continue

        # Clean cached events after 2 hours, default event ttl is 1 hour in K8s
        cached_event_uids = []
        logger.info('Wait 30 sec and check again.')
        time.sleep(30)

    logger.info("Done")


if __name__ == '__main__':
    main()
