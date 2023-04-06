# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function that records Personalize resource metrics

Lambda function designed to be called every five minutes to record campaign TPS
utilization metrics and recommender RRPS in CloudWatch. The metrics are used for
alarms and on the CloudWatch dashboard created by this application.
"""

import json
import os
import datetime
import sys
import math
import logging

from typing import Dict
from aws_lambda_powertools import Logger

from common import (
    PROJECT_NAME,
    ALARM_NAME_PREFIX,
    SNS_TOPIC_NAME,
    NOTIFICATIONS_RULE,
    NOTIFICATIONS_RULE_TARGET_ID,
    extract_region,
    get_client,
    get_configured_active_campaigns,
    get_configured_active_recommenders,
    put_event
)

logger = Logger()

MAX_METRICS_PER_CALL = 20
MIN_IDLE_THRESHOLD_HOURS = 1

ALARM_PERIOD_SECONDS = 300
ALARM_NAME_PREFIX_LOW_CAMPAIGN_UTILIZATION = ALARM_NAME_PREFIX + 'LowCampaignUtilization-'
ALARM_NAME_PREFIX_LOW_RECOMMENDER_UTILIZATION = ALARM_NAME_PREFIX + 'LowRecommenderUtilization-'
ALARM_NAME_PREFIX_IDLE_CAMPAIGN = ALARM_NAME_PREFIX + 'IdleCampaign-'
ALARM_NAME_PREFIX_IDLE_RECOMMENDER = ALARM_NAME_PREFIX + 'IdleRecommender-'

_topic_arn_by_region = {}

def get_recipe_arn(resource: Dict):
    recipe_arn = resource.get('recipeArn')
    if not recipe_arn and 'campaignArn' in resource:
        campaign_region = extract_region(resource['campaignArn'])
        personalize = get_client('personalize', campaign_region)

        response = personalize.describe_solution_version(solutionVersionArn = resource['solutionVersionArn'])

        recipe_arn = response['solutionVersion']['recipeArn']
        resource['recipeArn'] = recipe_arn

    return recipe_arn

def get_inference_metric_name(resource):
    metric_name = 'GetRecommendations'
    if 'campaignArn' in resource and get_recipe_arn(resource) == 'arn:aws:personalize:::recipe/aws-personalized-ranking':
        metric_name = 'GetPersonalizedRanking'

    return metric_name

def get_sum_requests_datapoints(resource, start_time, end_time, period):
    if 'campaignArn' in resource:
        arn_key = 'campaignArn'
        dim_name = 'CampaignArn'
    else:
        arn_key = 'recommenderArn'
        dim_name = 'RecommenderArn'

    resource_region = extract_region(resource[arn_key])
    cw = get_client(service_name = 'cloudwatch', region_name = resource_region)

    metric_name = get_inference_metric_name(resource)

    response = cw.get_metric_data(
        MetricDataQueries = [
            {
                'Id': 'm1',
                'MetricStat': {
                    'Metric': {
                        'Namespace': 'AWS/Personalize',
                        'MetricName': metric_name,
                        'Dimensions': [
                            {
                                'Name': dim_name,
                                'Value': resource[arn_key]
                            }
                        ]
                    },
                    'Period': period,
                    'Stat': 'Sum'
                },
                'ReturnData': True
            }
        ],
        StartTime = start_time,
        EndTime = end_time,
        ScanBy = 'TimestampDescending'
    )

    datapoints = []

    if response.get('MetricDataResults') and len(response['MetricDataResults']) > 0:
        results = response['MetricDataResults'][0]

        for idx, ts in enumerate(results['Timestamps']):
            datapoints.append({
                'Timestamp': ts,
                'Value': results['Values'][idx]
            })

    return datapoints

def get_sum_requests_by_hour(resource, start_time, end_time):
    datapoints = get_sum_requests_datapoints(resource, start_time, end_time, 3600)
    return datapoints

def get_total_requests(resource, start_time, end_time, period):
    datapoints = get_sum_requests_datapoints(resource, start_time, end_time, period)

    sum_requests = 0
    if datapoints:
        for datapoint in datapoints:
            sum_requests += datapoint['Value']

    return sum_requests

def get_average_tps(resource, start_time, end_time, period = ALARM_PERIOD_SECONDS):
    sum_requests = get_total_requests(resource, start_time, end_time, period)
    return sum_requests / period

def get_age_hours(resource):
    diff = datetime.datetime.now(datetime.timezone.utc) - resource['creationDateTime']
    days, seconds = diff.days, diff.seconds

    hours_age = days * 24 + seconds // 3600
    return hours_age

def get_last_update_age_hours(resource):
    hours_age = None
    if resource.get('lastUpdatedDateTime'):
        diff = datetime.datetime.now(datetime.timezone.utc) - resource['lastUpdatedDateTime']
        days, seconds = diff.days, diff.seconds

        hours_age = days * 24 + seconds // 3600
    return hours_age

def is_resource_updatable(resource):
    status = resource['status']
    updatable = status == 'ACTIVE' or status == 'CREATE FAILED'

    if updatable:
        if resource.get('latestCampaignUpdate'):
            status = resource['latestCampaignUpdate']['status']
            updatable = status == 'ACTIVE' or status == 'CREATE FAILED'
        elif resource.get('latestRecommenderUpdate'):
            status = resource['latestRecommenderUpdate']['status']
            updatable = status == 'ACTIVE' or status == 'CREATE FAILED'

    return updatable

def put_metrics(client, metric_datas):
    metric = {
        'Namespace': PROJECT_NAME,
        'MetricData': metric_datas
    }

    client.put_metric_data(**metric)
    logger.debug('Put data for %d metrics', len(metric_datas))

def append_metric(metric_datas_by_region, region, metric):
    metric_datas = metric_datas_by_region.get(region)

    if not metric_datas:
        metric_datas = []
        metric_datas_by_region[region] = metric_datas

    metric_datas.append(metric)

def notifications_rule_exists(events_client) -> bool:
    try:
        events_client.describe_rule(Name = NOTIFICATIONS_RULE)
        return True
    except events_client.exceptions.ResourceNotFoundException:
        return False

def get_notification_subscription(sns_client, topic_arn, endpoint: str) -> Dict:
    subs_paginator = sns_client.get_paginator('list_subscriptions_by_topic')
    for subs_page in subs_paginator.paginate(TopicArn = topic_arn):
        if subs_page.get('Subscriptions'):
            for sub in subs_page['Subscriptions']:
                if endpoint == sub.get('Endpoint'):
                    return sns_client.get_subscription_attributes(SubscriptionArn=sub['SubscriptionArn'])['Attributes']
    return None

def get_topic_arn(resource_region: str) -> str:
    # If the ARN has already been created/fetched, return it from cache.
    if resource_region in _topic_arn_by_region:
        logger.debug('Returning cached SNS topic ARN for region %s', resource_region)
        return _topic_arn_by_region[resource_region]

    sns = get_client(service_name = 'sns', region_name = resource_region)

    logger.info('Creating/fetching SNS topic ARN for topic %s in region %s', SNS_TOPIC_NAME, resource_region)
    response = sns.create_topic(Name = SNS_TOPIC_NAME)
    topic_arn = response['TopicArn']

    logger.info('Setting topic policy for SNS topic %s', topic_arn)
    sns.set_topic_attributes(
        TopicArn = topic_arn,
        AttributeName = 'Policy',
        AttributeValue = '''{
            "Version": "2008-10-17",
            "Id": "PublishPolicy",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {
                "Service": [
                    "cloudwatch.amazonaws.com",
                    "events.amazonaws.com"
                ]
                },
                "Action": [ "sns:Publish" ],
                "Resource": "%s"
            }]
        }''' % (topic_arn)
    )

    # Cache it so we avoid repeat calls while function is resident.
    _topic_arn_by_region[resource_region] = topic_arn

    events = get_client(service_name = 'events', region_name = resource_region)

    if not notifications_rule_exists(events):
        logger.info('EventBridge notifications rule %s does not exist; creating', NOTIFICATIONS_RULE)

        response = events.put_rule(
            Name = NOTIFICATIONS_RULE,
            EventPattern = '''{
                "detail-type": ["PersonalizeCampaignMinProvisionedTPSUpdated", "PersonalizeCampaignDeleted", "PersonalizeRecommenderMinRecommendationRPSUpdated", "PersonalizeRecommenderStopped"],
                "source": ["personalize.monitor"]
            }''',
            State = 'ENABLED',
            Description = 'Routes Personalize Monitor notifications to notification SNS topic'
        )

        logger.info('Setting target on notification rule')
        events.put_targets(
            Rule = NOTIFICATIONS_RULE,
            Targets = [{
                'Id': NOTIFICATIONS_RULE_TARGET_ID,
                'Arn': topic_arn
            }]
        )
    else:
        logger.info('EventBridge notification rule %s already exists', NOTIFICATIONS_RULE)

    notification_endpoint = os.environ.get('NotificationEndpoint')

    if notification_endpoint:
        logger.info('Verifying SNS topic subscription for %s', notification_endpoint)
        subscription = get_notification_subscription(sns, topic_arn, notification_endpoint)
        if subscription == None:
            logger.info('Subscribing endpoint %s to SNS topic %s', notification_endpoint, topic_arn)
            sns.subscribe(
                TopicArn = topic_arn,
                Protocol = 'email',
                Endpoint = notification_endpoint
            )
        elif subscription['PendingConfirmation'] == 'true':
            logger.warn('SNS topic subscription is still pending confirmation')
        else:
            logger.info('Endpoint is subscribed and confirmed for SNS topic')
    else:
        logger.warn('No notification endpoint specified at deployment so not adding subscriber')

    return topic_arn

def create_utilization_alarm(resource_region, resource, utilization_threshold_lower_bound):
    cw = get_client(service_name = 'cloudwatch', region_name = resource_region)

    if 'campaignArn' in resource:
        metric_name = 'campaignUtilization'
        arn_key = 'campaignArn'
        dim_name = 'CampaignArn'
        alarm_prefix = ALARM_NAME_PREFIX_LOW_CAMPAIGN_UTILIZATION
        # Only enable alarm actions when minTPS > 1 since we can't really do
        # anything to impact utilization by dropping minTPS. Let the idle
        # alarm handle abandoned campaigns/recommenders.
        enable_actions = resource['minProvisionedTPS'] > 1
    else:
        metric_name = 'recommenderUtilization'
        arn_key = 'recommenderArn'
        dim_name = 'RecommenderArn'
        alarm_prefix = ALARM_NAME_PREFIX_LOW_RECOMMENDER_UTILIZATION
        # Only enable alarm actions when minRPS > 1 since we can't really do
        # anything to impact utilization by dropping minTPS. Let the idle
        # alarm handle abandoned campaigns/recommenders.
        enable_actions = resource['recommenderConfig']['minRecommendationRequestsPerSecond'] > 1

    response = cw.describe_alarms_for_metric(
        MetricName = metric_name,
        Namespace = PROJECT_NAME,
        Dimensions=[
            {
                'Name': dim_name,
                'Value': resource[arn_key]
            },
        ]
    )

    alarm_name = alarm_prefix + resource['name']

    low_utilization_alarm_exists = False
    actions_currently_enabled = False

    for alarm in response['MetricAlarms']:
        if (alarm['AlarmName'].startswith(alarm_prefix) and
                alarm['ComparisonOperator'] in [ 'LessThanThreshold', 'LessThanOrEqualToThreshold' ]):
            alarm_name = alarm['AlarmName']
            low_utilization_alarm_exists = True
            actions_currently_enabled = alarm['ActionsEnabled']
            break

    alarm_created = False

    if not low_utilization_alarm_exists:
        logger.info('Creating lower bound utilization alarm for %s', resource[arn_key])

        topic_arn = get_topic_arn(resource_region)

        cw.put_metric_alarm(
            AlarmName = alarm_name,
            AlarmDescription = 'Alarms when utilization falls below threshold indicating possible over provisioning condition',
            ActionsEnabled = enable_actions,
            OKActions = [ topic_arn ],
            AlarmActions = [ topic_arn ],
            MetricName = metric_name,
            Namespace = PROJECT_NAME,
            Statistic = 'Average',
            Dimensions = [
                {
                    'Name': dim_name,
                    'Value': resource[arn_key]
                }
            ],
            Period = ALARM_PERIOD_SECONDS,
            EvaluationPeriods = 12, # last 60 minutes
            DatapointsToAlarm = 9,  # alarm state for 45 of last 60 minutes
            Threshold = utilization_threshold_lower_bound,
            ComparisonOperator = 'LessThanThreshold',
            TreatMissingData = 'missing',
            Tags=[
                {
                    'Key': 'CreatedBy',
                    'Value': PROJECT_NAME
                }
            ]
        )

        alarm_created = True
    elif enable_actions != actions_currently_enabled:
        # Toggle enable/disable actions for existing alarm.
        if enable_actions:
            cw.enable_alarm_actions(AlarmNames = [ alarm_name ])
        else:
            cw.disable_alarm_actions(AlarmNames = [ alarm_name ])

    return alarm_created

def create_idle_resource_alarm(resource_region, resource, idle_threshold_hours):
    cw = get_client(service_name = 'cloudwatch', region_name = resource_region)
    topic_arn = get_topic_arn(resource_region)

    metric_name = get_inference_metric_name(resource)

    if 'campaignArn' in resource:
        arn_key = 'campaignArn'
        dim_name = 'CampaignArn'
        alarm_prefix = ALARM_NAME_PREFIX_IDLE_CAMPAIGN
    else:
        arn_key = 'recommenderArn'
        dim_name = 'RecommenderArn'
        alarm_prefix = ALARM_NAME_PREFIX_IDLE_RECOMMENDER

    response = cw.describe_alarms_for_metric(
        MetricName = metric_name,
        Namespace = 'AWS/Personalize',
        Dimensions=[
            {
                'Name': dim_name,
                'Value': resource[arn_key]
            },
        ]
    )

    alarm_name = alarm_prefix + resource['name']

    idle_alarm_exists = False
    # Only enable actions when the campaign/recommender has existed at least as
    # long as the idle threshold. This is necessary since the alarm treats missing
    # data as breaching.
    enable_actions = get_age_hours(resource) >= idle_threshold_hours
    actions_currently_enabled = False

    for alarm in response['MetricAlarms']:
        if (alarm['AlarmName'].startswith(alarm_prefix) and
                alarm['ComparisonOperator'] == 'LessThanOrEqualToThreshold' and
                int(alarm['Threshold']) == 0):
            alarm_name = alarm['AlarmName']
            idle_alarm_exists = True
            actions_currently_enabled = alarm['ActionsEnabled']
            break

    alarm_created = False

    if not idle_alarm_exists:
        logger.info('Creating idle utilization alarm for %s', resource[arn_key])

        cw.put_metric_alarm(
            AlarmName = alarm_name,
            AlarmDescription = 'Alarms when utilization is idle for continguous length of time indicating potential abandoned campaign/recommender',
            ActionsEnabled = enable_actions,
            OKActions = [ topic_arn ],
            AlarmActions = [ topic_arn ],
            MetricName = metric_name,
            Namespace = 'AWS/Personalize',
            Statistic = 'Sum',
            Dimensions = [
                {
                    'Name': dim_name,
                    'Value': resource[arn_key]
                }
            ],
            Period = ALARM_PERIOD_SECONDS,
            EvaluationPeriods = int(((60 * 60) / ALARM_PERIOD_SECONDS) * idle_threshold_hours),
            Threshold = 0,
            ComparisonOperator = 'LessThanOrEqualToThreshold',
            TreatMissingData = 'breaching', # Won't get metric data for idle campaigns
            Tags=[
                {
                    'Key': 'CreatedBy',
                    'Value': PROJECT_NAME
                }
            ]
        )

        alarm_created = True
    elif enable_actions != actions_currently_enabled:
        # Toggle enable/disable actions for existing alarm.
        if enable_actions:
            cw.enable_alarm_actions(AlarmNames = [ alarm_name ])
        else:
            cw.disable_alarm_actions(AlarmNames = [ alarm_name ])

    return alarm_created

def divide_chunks(l, n):
    for i in range(0, len(l), n):
        yield l[i:i + n]

def perform_hourly_checks(resource_arn):
    ''' Hashes resource_arn across 10 minute intervals of the current hour so we spread out hourly checks '''
    num_slots = 6  # 60 mins / 10
    slot = sum(bytearray(resource_arn.encode('utf-8'))) % num_slots
    # Allow for match on first two minutes of 10 minute slot to account for CW event lag (assumes current schedule of every 5 mins).
    return datetime.datetime.now().minute in range(slot * 10, slot * 10 + 2)

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, _):
    auto_create_utilization_alarms = event.get('AutoCreateUtilizationAlarms')
    if not auto_create_utilization_alarms:
        auto_create_utilization_alarms = os.environ.get('AutoCreateUtilizationAlarms', 'yes').lower() in [ 'true', 'yes', '1' ]

    utilization_threshold_lower_bound = event.get('UtilizationThresholdAlarmLowerBound')
    if not utilization_threshold_lower_bound:
        utilization_threshold_lower_bound = float(os.environ.get('UtilizationThresholdAlarmLowerBound', '100.0'))

    auto_create_idle_alarms = event.get('AutoCreateIdleAlarms')
    if not auto_create_idle_alarms:
        auto_create_idle_alarms = os.environ.get('AutoCreateIdleAlarms', 'yes').lower() in [ 'true', 'yes', '1' ]

    auto_delete_idle_resources = event.get('AutoDeleteOrStopIdleResources')
    if not auto_delete_idle_resources:
        auto_delete_idle_resources = os.environ.get('AutoDeleteOrStopIdleResources', 'false').lower() in [ 'true', 'yes', '1' ]

    idle_resource_threshold_hours = event.get('IdleThresholdHours')
    if not idle_resource_threshold_hours:
        idle_resource_threshold_hours = int(os.environ.get('IdleThresholdHours', '24'))

    if idle_resource_threshold_hours < MIN_IDLE_THRESHOLD_HOURS:
        raise ValueError(f'"IdleThresholdHours" must be >= {MIN_IDLE_THRESHOLD_HOURS} hours')

    auto_adjust_min_tps = event.get('AutoAdjustMinTPS')
    if not auto_adjust_min_tps:
        auto_adjust_min_tps = os.environ.get('AutoAdjustMinTPS', 'yes').lower() in [ 'true', 'yes', '1' ]

    campaigns = get_configured_active_campaigns(event)
    recommenders = get_configured_active_recommenders(event)

    current_region = os.environ['AWS_REGION']

    metric_datas_by_region = {}

    append_metric(metric_datas_by_region, current_region, {
        'MetricName': 'monitoredResourceCount',
        'Value': len(campaigns) + len(recommenders),
        'Unit': 'Count'
    })

    resource_metrics_written = 0
    all_metrics_written = 0
    alarms_created = 0

    # Define our 5 minute window, ensuring it's on prior 5 minute boundary.
    end_time = datetime.datetime.now(datetime.timezone.utc)
    end_time = end_time.replace(microsecond=0,second=0, minute=end_time.minute - end_time.minute % 5)
    start_time = end_time - datetime.timedelta(minutes=5)

    logger.info('Retrieving minProvisionedTPS for %d active campaigns', len(campaigns))
    logger.info('Retrieving minRecommendationRequestsPerSecond for %d active recommenders', len(recommenders))

    for resource in campaigns + recommenders:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('Resource: %s', json.dumps(resource, indent = 2, default = str))

        is_campaign = 'campaignArn' in resource

        resource_arn = resource['campaignArn'] if is_campaign else resource['recommenderArn']
        resource_region = extract_region(resource_arn)

        min_tps = resource['minProvisionedTPS'] if is_campaign else resource['recommenderConfig']['minRecommendationRequestsPerSecond']

        append_metric(metric_datas_by_region, resource_region, {
            'MetricName': 'minProvisionedTPS' if is_campaign else 'minRecommendationRequestsPerSecond',
            'Dimensions': [
                {
                    'Name': 'CampaignArn' if is_campaign else 'RecommenderArn',
                    'Value': resource_arn
                }
            ],
            'Value': min_tps,
            'Unit': 'Count/Second'
        })

        tps = get_average_tps(resource, start_time, end_time)
        utilization = 0

        if tps:
            append_metric(metric_datas_by_region, resource_region, {
                'MetricName': 'averageTPS' if is_campaign else 'averageRPS',
                'Dimensions': [
                    {
                        'Name': 'CampaignArn' if is_campaign else 'RecommenderArn',
                        'Value': resource_arn
                    }
                ],
                'Value': tps,
                'Unit': 'Count/Second'
            })

            utilization = tps / min_tps * 100

        append_metric(metric_datas_by_region, resource_region, {
            'MetricName': 'campaignUtilization' if is_campaign else 'recommenderUtilization',
            'Dimensions': [
                {
                    'Name': 'CampaignArn' if is_campaign else 'RecommenderArn',
                    'Value': resource_arn
                }
            ],
            'Value': utilization,
            'Unit': 'Percent'
        })

        logger.debug(
            'Resource %s has current minTPS of %d and actual TPS of %s yielding %.2f%% utilization',
            resource_arn, min_tps, tps, utilization
        )
        resource_metrics_written += 1

        # Only do idle resource and minTPS adjustment checks once per hour for each campaign/recommender.
        perform_hourly_checks_this_run = perform_hourly_checks(resource_arn)

        # Determine how old the resource is and time since last update.
        resource_age_hours = get_age_hours(resource)
        resource_update_age_hours = get_last_update_age_hours(resource)

        resource_delete_stop_event_fired = False

        if utilization == 0 and perform_hourly_checks_this_run and auto_delete_idle_resources:
            # Resource is currently idle. Let's see if it's old enough and not being updated recently.
            logger.info(
                'Performing idle stop/delete check for %s; resource is %d hours old; last updated %s hours ago',
                resource_arn, resource_age_hours, resource_update_age_hours
            )

            if (resource_age_hours >= idle_resource_threshold_hours):

                # Resource has been around long enough. Let's see how long it's been idle.
                end_time_idle_check = datetime.datetime.now(datetime.timezone.utc)
                start_time_idle_check = end_time_idle_check - datetime.timedelta(hours = idle_resource_threshold_hours)
                period_idle_check = idle_resource_threshold_hours * 60 * 60

                total_requests = get_total_requests(resource, start_time_idle_check, end_time_idle_check, period_idle_check)

                if total_requests == 0:
                    if is_resource_updatable(resource):
                        if is_campaign:
                            detail_type = 'DeletePersonalizeCampaign'
                            reason = f'Campaign {resource_arn} has been idle for at least {idle_resource_threshold_hours} hours so initiating delete according to configuration.'
                        else:
                            detail_type = 'StopPersonalizeRecommender'
                            reason = f'Recommender {resource_arn} has been idle for at least {idle_resource_threshold_hours} hours so initiating stop according to configuration.'

                        logger.info(reason)

                        put_event(
                            detail_type = detail_type,
                            detail = json.dumps({
                                'ARN': resource_arn,
                                'Utilization': utilization,
                                'AgeHours': resource_age_hours,
                                'IdleThresholdHours': idle_resource_threshold_hours,
                                'TotalRequestsDuringIdleThresholdHours': total_requests,
                                'Reason': reason
                            }),
                            resources = [ resource_arn ]
                        )

                        resource_delete_stop_event_fired = True
                    else:
                        logger.warn(
                            'Resource %s has been idle for at least %d hours but its status will not allow it to be deleted/stopped on this run',
                            resource_arn, idle_resource_threshold_hours
                        )
                else:
                    logger.warn(
                        'Resource %s is currently idle but has had %d requests within the last %d hours so does not meet idle criteria for auto-deletion/auto-stop',
                        resource_arn, total_requests, idle_resource_threshold_hours
                    )
            else:
                logger.info(
                    'Resource %s is only %d hours old and last update %s hours old; too new to consider for auto-deletion/auto-stop',
                    resource_arn, resource_age_hours, resource_update_age_hours
                )

        if (not resource_delete_stop_event_fired and
                perform_hourly_checks_this_run and
                auto_adjust_min_tps and
                min_tps > 1):

            days_back = 14
            end_time_tps_check = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
            start_time_tps_check = end_time_tps_check - datetime.timedelta(days = days_back)

            datapoints = get_sum_requests_by_hour(resource, start_time_tps_check, end_time_tps_check)
            min_reqs = sys.maxsize
            max_reqs = total_reqs = total_avg_tps = min_avg_tps = max_avg_tps = 0

            for datapoint in datapoints:
                total_reqs += datapoint['Value']
                min_reqs = min(min_reqs, datapoint['Value'])
                max_reqs = max(max_reqs, datapoint['Value'])

            if len(datapoints) > 0:
                total_avg_tps = int(total_reqs / (len(datapoints) * 3600))
                min_avg_tps = int(min_reqs / 3600)
                max_avg_tps = int(max_reqs / 3600)

            logger.info(
                'Performing minTPS/minRPS adjustment check for %s; min/max/avg hourly TPS over last %d days for %d datapoints: %d/%d/%.2f',
                resource_arn, days_back, len(datapoints), min_avg_tps, max_avg_tps, total_avg_tps
            )

            min_age_to_update_hours = 24

            age_eligible = True

            if resource_age_hours < min_age_to_update_hours:
                logger.info(
                    'Resource %s is less than %d hours old so not eligible for minTPS/minRPS adjustment yet',
                    resource_arn, min_age_to_update_hours
                )
                age_eligible = False

            if age_eligible and min_avg_tps < min_tps:
                # Incrementally drop minTPS/minRPS.
                new_min_tps = max(1, int(math.floor(min_tps * .75)))

                if is_resource_updatable(resource):
                    reason = f'Step down adjustment of minTPS/minRPS for {resource_arn} down from {min_tps} to {new_min_tps} based on average hourly TPS low watermark of {min_avg_tps} over last {days_back} days'
                    logger.info(reason)

                    put_event(
                        detail_type = 'UpdatePersonalizeCampaignMinProvisionedTPS' if is_campaign else 'UpdatePersonalizeRecommenderMinRecommendationRPS',
                        detail = json.dumps({
                            'ARN': resource_arn,
                            'Utilization': utilization,
                            'AgeHours': resource_age_hours,
                            'CurrentMinTPS': min_tps,
                            'NewMinTPS': new_min_tps,
                            'MinAverageTPS': min_avg_tps,
                            'MaxAverageTPS': max_avg_tps,
                            'Datapoints': datapoints,
                            'Reason': reason
                        }, default = str),
                        resources = [ resource_arn ]
                    )
                else:
                    logger.warn(
                        'Resource %s could have its minTPS/minRPS adjusted down from %d to %d based on average hourly TPS low watermark over last %d days but its status will not allow it to be updated on this run',
                        resource_arn, min_tps, new_min_tps, days_back
                    )

        if not resource_delete_stop_event_fired:
            if auto_create_utilization_alarms:
                if create_utilization_alarm(resource_region, resource, utilization_threshold_lower_bound):
                    alarms_created += 1

            if auto_create_idle_alarms:
                if create_idle_resource_alarm(resource_region, resource, idle_resource_threshold_hours):
                    alarms_created += 1

    for region, metric_datas in metric_datas_by_region.items():
        cw = get_client(service_name = 'cloudwatch', region_name = region)

        metric_datas_chunks = divide_chunks(metric_datas, MAX_METRICS_PER_CALL)

        for metrics_datas_chunk in metric_datas_chunks:
            put_metrics(cw, metrics_datas_chunk)
            all_metrics_written += len(metrics_datas_chunk)

    outcome = f'Logged {all_metrics_written} TPS utilization metrics for {resource_metrics_written} active campaigns and recommenders; {alarms_created} alarms created'
    logger.info(outcome)

    if alarms_created > 0:
        # At least one new alarm was created so that likely means new campaigns were created too. Let's trigger the dashboard to be rebuilt.
        logger.info('Triggering rebuild of the CloudWatch dashboard since %d new alarm(s) were created', alarms_created)
        put_event(
            detail_type = 'BuildPersonalizeMonitorDashboard',
            detail = json.dumps({
                'Reason': f'Triggered rebuild due to {alarms_created} new alarm(s) being created'
            })
        )

    return outcome
