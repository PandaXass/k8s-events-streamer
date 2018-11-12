[![Build Status](https://api.travis-ci.org/PandaXass/k8s-events-streamer.svg?branch=master)](https://travis-ci.org/PandaXass/k8s-events-streamer)
[![Docker Automated build](https://img.shields.io/docker/automated/woshiywl1985/k8s-events-streamer.svg)](https://hub.docker.com/r/woshiywl1985/k8s-events-streamer)

# K8S events streamer

Based on https://github.com/Andrey9kin/k8s-events-to-slack-streamer.

Extend the feature of streaming k8s events to AWS CloudWatch logs.

Streams k8s events from k8s namespace to Slack channel as a Slack bot using incoming web hooks. No tokens needed.

# Known issue

* Model objects should tolerate None in place of empty lists https://github.com/kubernetes-client/python/issues/376.<br/>
For now we just ignore the _ValueError_ exceptions.

# Configuration

Configuration is done via env variables that you set in deployment or configmap.

* `K8S_EVENTS_STREAMER_CW_LOG_GROUP` - AWS CloudWatch log group name.
* `K8S_EVENTS_STREAMER_INCOMING_WEB_HOOK_URL` - Slack web hook URL where to send events.
* `K8S_EVENTS_STREAMER_NAMESPACE` - k8s namespace to collect events from. Will use `default` if not defined
* `K8S_EVENTS_STREAMER_DEBUG` - Enable debug print outs to the log. `False` if not defined. Set to `True` to enable.
* `K8S_EVENTS_STREAMER_SKIP_DELETE_EVENTS` - Skip all events of type DELETED by setting  env variable to `True`. `False` if not defined. Very useful since those events tells you that k8s event was deleted which has no value to you as operator.
* `K8S_EVENTS_STREAMER_LIST_OF_REASONS_TO_SKIP` - Skip events based on their `reason`. Should contain list of reasons separated by spaces. Very useful since there are a lot of events that doesn't tell you much like image pulled or replica scaled. Send all events if not defined. Recommended reasons to skip `'Scheduled ScalingReplicaSet Pulling Pulled Created Started Killing SuccessfulMountVolume SuccessfulUnMountVolume`. You can see more reasons [here](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/events/event.go)
* `K8S_EVENTS_STREAMER_USERS_TO_NOTIFY` - Mention users on warning events, ex `<@andrey9kin> <@slackbot>`. Note! It is important that you use `<>` around user name. Read more [here](https://api.slack.com/docs/message-formatting#linking_to_channels_and_users)

# Deployment

Intention is that you run streamer container in your k8s cluster. Take a look on example [deployment yaml file](example-deployment.yaml)

# Example message

![Example](/example.png)
