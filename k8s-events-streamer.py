#!/usr/bin/env python3

import os
import time
import json
import logging
import requests
import boto3
import kubernetes

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


def is_reason_in_skip_list(event, skip_list):
    return True if event['object'].reason in skip_list else False


def format_k8s_event_to_slack_message(event_object, notify=''):
    event = event_object['object']
    message = {
        'attachments': [{
            'color': '#36a64f',
            'title': event.message,
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
        logging.basicConfig(level=logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    logger.info("Reading configuration...")
    aws_region = os.environ.get('K8S_EVENTS_STREAMER_AWS_REGION', 'us-east-1')
    k8s_namespace_name = os.environ.get(
        'K8S_EVENTS_STREAMER_NAMESPACE', 'default')
    skip_delete_events = os.environ.get(
        'K8S_EVENTS_STREAMER_SKIP_DELETE_EVENTS', False)
    reasons_to_skip = os.environ.get(
        'K8S_EVENTS_STREAMER_LIST_OF_REASONS_TO_SKIP', '').split()

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
        for event in k8s_watch.stream(v1.list_namespaced_event, k8s_namespace_name):
            logger.debug(str(event))
            if is_message_type_delete(event) and skip_delete_events != False:
                logger.debug(
                    'Event type DELETED and skip deleted events is enabled. Skip this one')
                continue
            if is_reason_in_skip_list(event, reasons_to_skip) == True:
                logger.debug('Event reason is in the skip list. Skip it')
                continue

            if cw_log_group:
                pod = event['object'].involved_object.name
                kind = event['object'].involved_object.kind
                creation_timestamp = int(
                    event['object'].metadata.creation_timestamp.timestamp())
                cw_log_stream = '{}/{}/{}'.format(
                    k8s_namespace_name, kind, pod)

                r = client_cw_logs.describe_log_streams(
                    logGroupName=cw_log_group, logStreamNamePrefix=cw_log_stream, limit=1)
                if not r['logStreams']:
                    logger.info('Create cloudwatch log stream {} in log group {}'.format(
                        cw_log_stream, cw_log_group))
                    client_cw_logs.create_log_stream(
                        logGroupName=cw_log_group, logStreamName=cw_log_stream)

                if 'uploadSequenceToken' in r['logStreams'][0]:
                    client_cw_logs.put_log_events(
                        logGroupName=cw_log_group,
                        logStreamName=cw_log_stream,
                        logEvents=[
                            {
                                'timestamp': creation_timestamp,
                                'message': str(event)
                            }
                        ],
                        sequenceToken=r['logStreams'][0]['uploadSequenceToken'])
                else:
                    client_cw_logs.put_log_events(
                        logGroupName=cw_log_group,
                        logStreamName=cw_log_stream,
                        logEvents=[
                            {
                                'timestamp': creation_timestamp,
                                'message': str(event)
                            }
                        ])
            if slack_web_hook_url:
                message = format_k8s_event_to_slack_message(
                    event, users_to_notify)
                post_slack_message(slack_web_hook_url, message)
        logger.info('No more events. Wait 30 sec and check again')
        time.sleep(30)

    logger.info("Done")


if __name__ == '__main__':
    main()
