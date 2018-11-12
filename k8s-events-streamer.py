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

# # Workaround for https://github.com/kubernetes-client/python/issues/376
# from kubernetes.client.models.v1_object_reference import V1ObjectReference
# from kubernetes.client.models.v1_event import V1Event


# def set_involved_object(self, involved_object):
#     if involved_object is None:
#         involved_object = V1ObjectReference()
#     self._involved_object = involved_object


# setattr(V1Event, 'involved_object', property(
#     fget=V1Event.involved_object.fget, fset=set_involved_object))
# # End of workaround

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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


def is_message_type_delete(event):
    return True if event['type'] == 'DELETED' else False


# def is_reason_in_skip_list(event, skip_list):
#     return True if event['object'].reason in skip_list else False


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
        message['attachments'][0]['color'] = '#cc4d26'
        if notify != '':
            message['text'] = '{} there is a warning for you to check'.format(
                notify)

    return json.dumps(message)


def main():

    if os.environ.get('K8S_EVENTS_STREAMER_DEBUG', False):
        logger.setLevel(logging.DEBUG)
        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    logger.info("Reading configuration...")
    k8s_cluster_name = read_env_variable_or_die(
        'K8S_EVENTS_STREAMER_CLUSTER_NAME')
    aws_region = os.environ.get('K8S_EVENTS_STREAMER_AWS_REGION', 'us-east-1')
    k8s_namespace_name = os.environ.get(
        'K8S_EVENTS_STREAMER_NAMESPACE', 'default')
    skip_delete_events = os.environ.get(
        'K8S_EVENTS_STREAMER_SKIP_DELETE_EVENTS', False)
    # reasons_to_skip = os.environ.get(
    #     'K8S_EVENTS_STREAMER_LIST_OF_REASONS_TO_SKIP', '').split()
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

    if cw_log_group:
        client_cw_logs = boto3.client('logs', region_name=aws_region)
    while True:
        logger.info("Processing events...")
        try:
            events = k8s_watch.stream(
                v1.list_namespaced_event, k8s_namespace_name)
        except ValueError as e:
            logger.error(e)
            continue

        for event in events:
            logger.debug(str(event))
            if not event['object'].involved_object:
                logger.debug(
                    'Found empty involved_object in the event. Skip this one.'
                )
                continue
            if is_message_type_delete(event) and skip_delete_events != False:
                logger.debug(
                    'Event type DELETED and skip deleted events is enabled. Skip this one.')
                continue
            # if is_reason_in_skip_list(event, reasons_to_skip) == True:
            #     logger.debug('Event reason is in the skip list. Skip it')
            #     continue
            if is_reason_in_include_list(event, reasons_to_include) == False:
                logger.debug(
                    'Event reason is not in the include list. Skip this one.')
                continue

            if cw_log_group:
                pod = event['object'].involved_object.name
                kind = event['object'].involved_object.kind
                namespace = event['object'].involved_object.namespace
                creation_epoch_ms = int(
                    event['object'].metadata.creation_timestamp.timestamp()) * 1000
                cw_log_stream = '{}/{}/{}'.format(
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

            if slack_web_hook_url:
                message = format_k8s_event_to_slack_message(
                    event, k8s_cluster_name, users_to_notify)
                post_slack_message(slack_web_hook_url, message)

        logger.info('No more events. Wait 30 sec and check again')
        time.sleep(30)

    logger.info("Done")


if __name__ == '__main__':
    main()
