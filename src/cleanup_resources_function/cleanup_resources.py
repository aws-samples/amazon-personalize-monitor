# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cleans up resources created by this application outside of CloudFormation

This function is called as a CloudFormation custom resource.
"""

from crhelper import CfnResource
from aws_lambda_powertools import Logger

from common import (
    PROJECT_NAME,
    ALARM_NAME_PREFIX,
    extract_region,
    get_client,
    determine_campaign_arns
)

logger = Logger()
helper = CfnResource()

@helper.delete
def delete_resource(event, _):
    campaign_arns = determine_campaign_arns(event.get('ResourceProperties'))

    logger.debug('Campaigns to check for resources to delete: %s', campaign_arns)

    regions = set()

    for campaign_arn in campaign_arns:
        regions.add(extract_region(campaign_arn))

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
            logger.info('Deleting CloudWatch alarms in %s for campaigns %s: %s', region, campaign_arns, alarm_names_to_delete)
            cw.delete_alarms(AlarmNames=list(alarm_names_to_delete))
            alarms_deleted += len(alarm_names_to_delete)

    logger.info('Deleted %d alarms', alarms_deleted)

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, context):
    helper(event, context)