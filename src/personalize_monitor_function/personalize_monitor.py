# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function that records Personalize resource metrics

Lambda function designed to be called every five minutes to record campaign TPS 
utilization metrics in CloudWatch. The metrics are used for alarms and on the 
CloudWatch dashboard created by this application.
"""

import json
import boto3
import os
import datetime
import sys
import math

from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

from common import (
    PROJECT_NAME,
    ALARM_NAME_PREFIX,
    extract_region,
    get_client,
    determine_campaign_arns,
    get_configured_active_campaigns,
    put_event
)

logger = Logger()

MAX_METRICS_PER_CALL = 20
MIN_IDLE_CAMPAIGN_THRESHOLD_HOURS = 1

ALARM_PERIOD_SECONDS = 300
ALARM_NAME_PREFIX_LOW_UTILIZATION = ALARM_NAME_PREFIX + 'LowCampaignUtilization-'
ALARM_NAME_PREFIX_IDLE = ALARM_NAME_PREFIX + 'IdleCampaign-'

def get_campaign_recipe_arn(campaign):
    recipe_arn = campaign.get('recipeArn')
    if not recipe_arn:
        campaign_region = extract_region(campaign['campaignArn'])
        personalize = get_client('personalize', campaign_region)

        response = personalize.describe_solution_version(solutionVersionArn = campaign['solutionVersionArn'])

        recipe_arn = response['solutionVersion']['recipeArn']
        campaign['recipeArn'] = recipe_arn

    return recipe_arn

def get_campaign_inference_metric_name(campaign):
    metric_name = 'GetRecommendations'
    if get_campaign_recipe_arn(campaign) == 'arn:aws:personalize:::recipe/aws-personalized-ranking':
        metric_name = 'GetPersonalizedRanking'

    return metric_name

def get_campaign_sum_requests_datapoints(campaign, start_time, end_time, period):
    campaign_region = extract_region(campaign['campaignArn'])
    cw = get_client(service_name = 'cloudwatch', region_name = campaign_region)

    metric_name = get_campaign_inference_metric_name(campaign)

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
                                'Name': 'CampaignArn',
                                'Value': campaign['campaignArn']
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

def get_campaign_sum_requests_by_hour(campaign, start_time, end_time):
    datapoints = get_campaign_sum_requests_datapoints(campaign, start_time, end_time, 3600)
    return datapoints

def get_campaign_total_requests(campaign, start_time, end_time, period):
    datapoints = get_campaign_sum_requests_datapoints(campaign, start_time, end_time, period)

    sum_requests = 0
    if datapoints:
        for datapoint in datapoints:
            sum_requests += datapoint['Value']
        
    return sum_requests

def get_campaign_average_tps(campaign, start_time, end_time, period = ALARM_PERIOD_SECONDS):
    sum_requests = get_campaign_total_requests(campaign, start_time, end_time, period)
    return sum_requests / period

def get_campaign_age_hours(campaign):
    diff = datetime.datetime.now(datetime.timezone.utc) - campaign['creationDateTime']
    days, seconds = diff.days, diff.seconds

    hours_age = days * 24 + seconds // 3600
    return hours_age

def get_campaign_last_update_age_hours(campaign):
    hours_age = None
    if campaign.get('lastUpdatedDateTime'):
        diff = datetime.datetime.now(datetime.timezone.utc) - campaign['lastUpdatedDateTime']
        days, seconds = diff.days, diff.seconds

        hours_age = days * 24 + seconds // 3600
    return hours_age

def is_campaign_updatable(campaign):
    status = campaign['status']
    updatable = status == 'ACTIVE' or status == 'CREATE FAILED'

    if updatable and campaign.get('latestCampaignUpdate'):
        status = campaign['latestCampaignUpdate']['status']
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

def create_utilization_alarm(campaign_region, campaign, utilization_threshold_lower_bound):
    cw = get_client(service_name = 'cloudwatch', region_name = campaign_region)

    response = cw.describe_alarms_for_metric(
        MetricName = 'campaignUtilization',
        Namespace = PROJECT_NAME,
        Dimensions=[
            {
                'Name': 'CampaignArn',
                'Value': campaign['campaignArn']
            },
        ]
    )

    alarm_name = ALARM_NAME_PREFIX_LOW_UTILIZATION + campaign['name']

    low_utilization_alarm_exists = False
    # Only enable alarm actions when minTPS > 1 since we can't really do 
    # anything to impact utilization by dropping minTPS. Let the idle 
    # campaign alarm handle abandoned campaigns. 
    enable_actions = campaign['minProvisionedTPS'] > 1
    actions_currently_enabled = False

    for alarm in response['MetricAlarms']:
        if (alarm['AlarmName'].startswith(ALARM_NAME_PREFIX_LOW_UTILIZATION) and
                alarm['ComparisonOperator'] in [ 'LessThanThreshold', 'LessThanOrEqualToThreshold' ]):
            alarm_name = alarm['AlarmName']
            low_utilization_alarm_exists = True
            actions_currently_enabled = alarm['ActionsEnabled']
            break

    alarm_created = False

    if not low_utilization_alarm_exists:
        logger.info('Creating lower bound utilization alarm for %s', campaign['campaignArn'])

        topic_arn = os.environ['NotificationsTopic']

        cw.put_metric_alarm(
            AlarmName = alarm_name,
            AlarmDescription = 'Alarms when campaign utilization falls below threashold indicating possible over provisioning condition',
            ActionsEnabled = enable_actions,
            OKActions = [ topic_arn ],
            AlarmActions = [ topic_arn ],
            MetricName = 'campaignUtilization',
            Namespace = PROJECT_NAME,
            Statistic = 'Average',
            Dimensions = [
                {
                    'Name': 'CampaignArn',
                    'Value': campaign['campaignArn']
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

def create_idle_campaign_alarm(campaign_region, campaign, idle_campaign_threshold_hours):
    cw = get_client(service_name = 'cloudwatch', region_name = campaign_region)
    topic_arn = os.environ['NotificationsTopic']

    metric_name = get_campaign_inference_metric_name(campaign)

    response = cw.describe_alarms_for_metric(
        MetricName = metric_name,
        Namespace = 'AWS/Personalize',
        Dimensions=[
            {
                'Name': 'CampaignArn',
                'Value': campaign['campaignArn']
            },
        ]
    )

    alarm_name = ALARM_NAME_PREFIX_IDLE + campaign['name']

    idle_alarm_exists = False
    # Only enable actions when the campaign has existed at least as long as 
    # the idle threshold. This is necessary since the alarm treats missing 
    # data as breaching.
    enable_actions = get_campaign_age_hours(campaign) >= idle_campaign_threshold_hours
    actions_currently_enabled = False

    for alarm in response['MetricAlarms']:
        if (alarm['AlarmName'].startswith(ALARM_NAME_PREFIX_IDLE) and
                alarm['ComparisonOperator'] == 'LessThanOrEqualToThreshold' and
                int(alarm['Threshold']) == 0):
            alarm_name = alarm['AlarmName']
            idle_alarm_exists = True
            actions_currently_enabled = alarm['ActionsEnabled']
            break

    alarm_created = False

    if not idle_alarm_exists:
        logger.info('Creating idle utilization alarm for %s', campaign['campaignArn'])

        cw.put_metric_alarm(
            AlarmName = alarm_name,
            AlarmDescription = 'Alarms when campaign utilization is idle for continguous length of time indicating potential abandoned campaign',
            ActionsEnabled = enable_actions,
            OKActions = [ topic_arn ],
            AlarmActions = [ topic_arn ],
            MetricName = metric_name,
            Namespace = 'AWS/Personalize',
            Statistic = 'Sum',
            Dimensions = [
                {
                    'Name': 'CampaignArn',
                    'Value': campaign['campaignArn']
                }
            ],
            Period = ALARM_PERIOD_SECONDS,
            EvaluationPeriods = int(((60 * 60) / ALARM_PERIOD_SECONDS) * idle_campaign_threshold_hours),
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

def perform_hourly_checks(campaign_arn):
    ''' Hashes campaign_arn across 10 minute intervals of the current hour so we spread out campaign hourly checks '''
    num_slots = 6  # 60 mins / 10
    slot = sum(bytearray(campaign_arn.encode('utf-8'))) % num_slots
    # Allow for match on first two minutes of 10 minute slot to account for CW event lag (assumes current schedule of every 5 mins).
    return datetime.datetime.now().minute in range(slot * 10, slot * 10 + 2)

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, context):
    auto_create_utilization_alarms = event.get('AutoCreateCampaignUtilizationAlarms')
    if not auto_create_utilization_alarms:
        auto_create_utilization_alarms = os.environ.get('AutoCreateCampaignUtilizationAlarms', 'yes').lower() in [ 'true', 'yes', '1' ]

    utilization_threshold_lower_bound = event.get('CampaignThresholdAlarmLowerBound')
    if not utilization_threshold_lower_bound:
        utilization_threshold_lower_bound = float(os.environ.get('CampaignThresholdAlarmLowerBound', '100.0'))

    auto_create_idle_alarms = event.get('AutoCreateIdleCampaignAlarms')
    if not auto_create_idle_alarms:
        auto_create_idle_alarms = os.environ.get('AutoCreateIdleCampaignAlarms', 'yes').lower() in [ 'true', 'yes', '1' ]

    auto_delete_idle_campaigns = event.get('AutoDeleteIdleCampaigns')
    if not auto_delete_idle_campaigns:
        auto_delete_idle_campaigns = os.environ.get('AutoDeleteIdleCampaigns', 'false').lower() in [ 'true', 'yes', '1' ]

    idle_campaign_threshold_hours = event.get('IdleCampaignThresholdHours')
    if not idle_campaign_threshold_hours:
        idle_campaign_threshold_hours = int(os.environ.get('IdleCampaignThresholdHours', '24'))

    if idle_campaign_threshold_hours < MIN_IDLE_CAMPAIGN_THRESHOLD_HOURS:
        raise ValueError(f'"IdleCampaignThresholdHours" must be >= {MIN_IDLE_CAMPAIGN_THRESHOLD_HOURS} hours')

    auto_adjust_campaign_tps = event.get('AutoAdjustCampaignMinProvisionedTPS')
    if not auto_adjust_campaign_tps:
        auto_adjust_campaign_tps = os.environ.get('AutoAdjustCampaignMinProvisionedTPS', 'yes').lower() in [ 'true', 'yes', '1' ]

    campaigns = get_configured_active_campaigns(event)
    
    logger.info('Retrieving minProvisionedTPS for %d active campaigns', len(campaigns))

    current_region = os.environ['AWS_REGION']
    
    metric_datas_by_region = {}

    append_metric(metric_datas_by_region, current_region, {
        'MetricName': 'monitoredCampaignCount',
        'Value': len(campaigns),
        'Unit': 'Count'
    })
    
    campaign_metrics_written = 0
    all_metrics_written = 0
    alarms_created = 0

    # Define our 5 minute window, ensuring it's on prior 5 minute boundary.
    end_time = datetime.datetime.now(datetime.timezone.utc)
    end_time = end_time.replace(microsecond=0,second=0, minute=end_time.minute - end_time.minute % 5)
    start_time = end_time - datetime.timedelta(minutes=5)

    for campaign in campaigns:
        campaign_arn = campaign['campaignArn']
        campaign_region = extract_region(campaign_arn)

        min_provisioned_tps = campaign['minProvisionedTPS']
        
        append_metric(metric_datas_by_region, campaign_region, {
            'MetricName': 'minProvisionedTPS',
            'Dimensions': [
                {
                    'Name': 'CampaignArn',
                    'Value': campaign_arn
                }
            ],
            'Value': min_provisioned_tps,
            'Unit': 'Count/Second'
        })
        
        tps = get_campaign_average_tps(campaign, start_time, end_time)
        utilization = 0

        if tps:
            append_metric(metric_datas_by_region, campaign_region, {
                'MetricName': 'averageTPS',
                'Dimensions': [
                    {
                        'Name': 'CampaignArn',
                        'Value': campaign_arn
                    }
                ],
                'Value': tps,
                'Unit': 'Count/Second'
            })
            
            utilization = tps / min_provisioned_tps * 100

        append_metric(metric_datas_by_region, campaign_region, {
            'MetricName': 'campaignUtilization',
            'Dimensions': [
                {
                    'Name': 'CampaignArn',
                    'Value': campaign_arn
                }
            ],
            'Value': utilization,
            'Unit': 'Percent'
        })
            
        logger.debug(
            'Campaign %s has current minProvisionedTPS of %d and actual TPS of %s yielding %.2f%% utilization', 
            campaign_arn, min_provisioned_tps, tps, utilization
        )
        campaign_metrics_written += 1

        # Only do idle campaign and minProvisionedTPS adjustment checks once per hour for each campaign.
        perform_hourly_checks_this_run = perform_hourly_checks(campaign_arn)

        # Determine how old the campaign is and time since last update.
        campaign_age_hours = get_campaign_age_hours(campaign)
        campaign_update_age_hours = get_campaign_last_update_age_hours(campaign)

        campaign_delete_event_fired = False

        if utilization == 0 and perform_hourly_checks_this_run and auto_delete_idle_campaigns:
            # Campaign is currently idle. Let's see if it's old enough and not being updated recently.
            logger.info(
                'Performing idle delete check for campaign %s; campaign is %d hours old; last updated %s hours ago', 
                campaign_arn, campaign_age_hours, campaign_update_age_hours
            )

            if (campaign_age_hours >= idle_campaign_threshold_hours):

                # Campaign has been around long enough. Let's see how long it's been idle.
                end_time_idle_check = datetime.datetime.now(datetime.timezone.utc)
                start_time_idle_check = end_time_idle_check - datetime.timedelta(hours = idle_campaign_threshold_hours)
                period_idle_check = idle_campaign_threshold_hours * 60 * 60

                total_requests = get_campaign_total_requests(campaign, start_time_idle_check, end_time_idle_check, period_idle_check)

                if total_requests == 0:
                    if is_campaign_updatable(campaign):
                        reason = f'Campaign {campaign_arn} has been idle for at least {idle_campaign_threshold_hours} hours so initiating delete according to configuration.'

                        logger.info(reason)

                        put_event(
                            detail_type = 'DeletePersonalizeCampaign',
                            detail = json.dumps({
                                'CampaignARN': campaign_arn,
                                'CampaignUtilization': utilization,
                                'CampaignAgeHours': campaign_age_hours,
                                'IdleCampaignThresholdHours': idle_campaign_threshold_hours,
                                'TotalRequestsDuringIdleThresholdHours': total_requests,
                                'Reason': reason
                            }),
                            resources = [ campaign_arn ]
                        )

                        campaign_delete_event_fired = True
                    else:
                        logger.warn(
                            'Campaign %s has been idle for at least %d hours but its status will not allow it to be deleted on this run', 
                            campaign_arn, idle_campaign_threshold_hours
                        )
                else:
                    logger.warn(
                        'Campaign %s is currently idle but has had %d requests within the last %d hours so does not meet idle criteria for auto-deletion', 
                        campaign_arn, total_requests, idle_campaign_threshold_hours
                    )
            else:
                logger.info(
                    'Campaign %s is only %d hours old and last update %s hours old; too new to consider for auto-deletion', 
                    campaign_arn, campaign_age_hours, campaign_update_age_hours
                )

        if (not campaign_delete_event_fired and 
                perform_hourly_checks_this_run and 
                auto_adjust_campaign_tps and 
                min_provisioned_tps > 1):

            days_back = 14
            end_time_tps_check = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
            start_time_tps_check = end_time_tps_check - datetime.timedelta(days = days_back)

            datapoints = get_campaign_sum_requests_by_hour(campaign, start_time_tps_check, end_time_tps_check)
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
                'Performing minProvisionedTPS adjustment check for campaign %s; min/max/avg hourly TPS over last %d days for %d datapoints: %d/%d/%.2f', 
                campaign_arn, days_back, len(datapoints), min_avg_tps, max_avg_tps, total_avg_tps
            )

            min_age_to_update_hours = 24

            age_eligible = True

            if campaign_age_hours < min_age_to_update_hours:
                logger.info(
                    'Campaign %s is less than %d hours old so not eligible for minProvisionedTPS adjustment yet', 
                    campaign_arn, min_age_to_update_hours
                )
                age_eligible = False

            if age_eligible and min_avg_tps < min_provisioned_tps:
                # Incrementally drop minProvisionedTPS.
                new_min_tps = max(1, int(math.floor(min_provisioned_tps * .75)))

                if is_campaign_updatable(campaign):
                    reason = f'Step down adjustment of minProvisionedTPS for campaign {campaign_arn} down from {min_provisioned_tps} to {new_min_tps} based on average hourly TPS low watermark of {min_avg_tps} over last {days_back} days'
                    logger.info(reason)

                    put_event(
                        detail_type = 'UpdatePersonalizeCampaignMinProvisionedTPS',
                        detail = json.dumps({
                            'CampaignARN': campaign_arn,
                            'CampaignUtilization': utilization,
                            'CampaignAgeHours': campaign_age_hours,
                            'CurrentProvisionedTPS': min_provisioned_tps,
                            'MinProvisionedTPS': new_min_tps,
                            'MinAverageTPS': min_avg_tps,
                            'MaxAverageTPS': max_avg_tps,
                            'Datapoints': datapoints,
                            'Reason': reason
                        }, default = str),
                        resources = [ campaign_arn ]
                    )
                else:
                    logger.warn(
                        'Campaign %s could have its minProvisionedTPS adjusted down from %d to %d based on average hourly TPS low watermark over last %d days but its status will not allow it to be updated on this run', 
                        campaign_arn, min_provisioned_tps, new_min_tps, days_back
                    )

        if not campaign_delete_event_fired:
            if auto_create_utilization_alarms:
                if create_utilization_alarm(campaign_region, campaign, utilization_threshold_lower_bound):
                    alarms_created += 1

            if auto_create_idle_alarms:
                if create_idle_campaign_alarm(campaign_region, campaign, idle_campaign_threshold_hours):
                    alarms_created += 1

    for region, metric_datas in metric_datas_by_region.items():
        cw = get_client(service_name = 'cloudwatch', region_name = region)

        metric_datas_chunks = divide_chunks(metric_datas, MAX_METRICS_PER_CALL)

        for metrics_datas_chunk in metric_datas_chunks:
            put_metrics(cw, metrics_datas_chunk)
            all_metrics_written += len(metrics_datas_chunk)

    outcome = f'Logged {all_metrics_written} TPS utilization metrics for {campaign_metrics_written} active campaigns; {alarms_created} alarms created'
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
