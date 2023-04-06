# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cleans up resources created by this application outside of CloudFormation

This function is called as a CloudFormation custom resource.
"""

import boto3

from crhelper import CfnResource
from aws_lambda_powertools import Logger

from common import (
    PROJECT_NAME,
    ALARM_NAME_PREFIX,
    SNS_TOPIC_NAME,
    NOTIFICATIONS_RULE,
    NOTIFICATIONS_RULE_TARGET_ID,
    extract_region,
    get_client,
    determine_campaign_arns,
    determine_recommender_arns
)

logger = Logger()
helper = CfnResource()

sts = boto3.client('sts')
account_id = sts.get_caller_identity()['Account']

@helper.delete
def delete_resources(event, _):
    campaign_arns = determine_campaign_arns(event.get('ResourceProperties'))
    recommender_arns = determine_recommender_arns(event.get('ResourceProperties'))

    logger.debug('Campaigns to check for resources to delete: %s', campaign_arns)
    logger.debug('Recommenders to check for resources to delete: %s', recommender_arns)

    regions = set()

    for campaign_arn in campaign_arns:
        regions.add(extract_region(campaign_arn))

    for recommender_arn in recommender_arns:
        regions.add(extract_region(recommender_arn))

    logger.debug('Regions to check for resources to delete: %s', regions)

    alarms_deleted = 0

    for region in regions:
        cw = get_client(service_name = 'cloudwatch', region_name = region)

        alarm_names_to_delete = set()

        alarms_paginator = cw.get_paginator('describe_alarms')
        for alarms_page in alarms_paginator.paginate(AlarmNamePrefix = ALARM_NAME_PREFIX, AlarmTypes=['MetricAlarm']):
            for alarm in alarms_page['MetricAlarms']:
                tags_response = cw.list_tags_for_resource(ResourceARN = alarm['AlarmArn'])

                for tag in tags_response['Tags']:
                    if tag['Key'] == 'CreatedBy' and tag['Value'] == PROJECT_NAME:
                        alarm_names_to_delete.add(alarm['AlarmName'])
                        break

        if alarm_names_to_delete:
            # FUTURE: max check of 100
            logger.info('Deleting CloudWatch alarms in %s for campaigns %s and recommenders %s: %s', region, campaign_arns, recommender_arns, alarm_names_to_delete)
            cw.delete_alarms(AlarmNames=list(alarm_names_to_delete))
            alarms_deleted += len(alarm_names_to_delete)

        events = get_client(service_name = 'events', region_name = region)
        try:
            logger.info('Removing targets from EventBridge notification rule %s for region %s', NOTIFICATIONS_RULE, region)
            events.remove_targets(
                Rule = NOTIFICATIONS_RULE,
                Ids = [ NOTIFICATIONS_RULE_TARGET_ID ]
            )
        except events.exceptions.ResourceNotFoundException:
            logger.warn('EventBridge notification rule targets not found')

        try:
            logger.info('Deleting EventBridge notification rule %s for region %s', NOTIFICATIONS_RULE, region)
            events.delete_rule(Name = NOTIFICATIONS_RULE)
        except events.exceptions.ResourceNotFoundException:
            logger.warn('EventBridge notification rule %s does not exist', NOTIFICATIONS_RULE)

        sns = get_client(service_name = 'sns', region_name = region)
        topic_arn = f'arn:aws:sns:{region}:{account_id}:{SNS_TOPIC_NAME}'
        logger.info('Deleting SNS topic %s', topic_arn)
        # This API is idempotent so will not fail if topic does not exist
        sns.delete_topic(TopicArn = topic_arn)

    logger.info('Deleted %d alarms', alarms_deleted)

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, context):
    helper(event, context)