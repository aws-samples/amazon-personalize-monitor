# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function that is used to delete a Personalize campaign based on prolonged idle time 
and according to configuration to automatically delete campaigns under these conditions.
"""

import json
import boto3
import os
import json
import logging

from aws_lambda_powertools import Logger

from common import (
    extract_region,
    get_client,
    put_event
)

logger = Logger()

def delete_alarms_for_campaign(campaign_arn):
    cw = get_client(service_name = 'cloudwatch', region_name = extract_region(campaign_arn))

    alarm_names_to_delete = set()

    alarms_paginator = cw.get_paginator('describe_alarms')
    for alarms_page in alarms_paginator.paginate(AlarmNamePrefix = ALARM_NAME_PREFIX, AlarmTypes=['MetricAlarm']):
        for alarm in alarms_page['MetricAlarms']:
            for dim in alarm['Dimensions']:
                if dim['Name'] == 'CampaignArn' and dim['Value'] == campaign_arn:
                    tags_response = cw.list_tags_for_resource(ResourceARN = alarm['AlarmArn'])

                    for tag in tags_response['Tags']:
                        if tag['Key'] == 'CreatedBy' and tag['Value'] == PROJECT_NAME:
                            alarm_names_to_delete.add(alarm['AlarmName'])
                            break

    if alarm_names_to_delete:
        # FUTURE: max check of 100
        logger.info('Deleting CloudWatch alarms for campaign %s: %s', campaign_arn, alarm_names_to_delete)
        cw.delete_alarms(AlarmNames=list(alarm_names_to_delete))
        alarms_deleted += len(alarm_names_to_delete)
    else:
        logger.info('No CloudWatch alarms to delete for campaign %s', campaign_arn)

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event, context):
    ''' Initiates the delete of a Personalize campaign '''
    if event.get('detail'):
        campaign_arn = event['detail']['CampaignARN']
        reason = event['detail']['Reason']
    else:
        campaign_arn = event['CampaignARN']
        reason = event.get('Reason')
    
    region = extract_region(campaign_arn)
    if not region:
        raise Exception('Region could not be extracted from campaign_arn')
    
    personalize = get_client(service_name = 'personalize', region_name = region)

    response = personalize.delete_campaign(campaignArn = campaign_arn)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(json.dumps(response, indent = 2, default = str))

    if not reason:
        reason = f'Amazon Personalize campaign {campaign_arn} deletion initiated (reason unspecified)'

    put_event(
        detail_type = 'PersonalizeCampaignDeleted',
        detail = json.dumps({
            'CampaignARN': campaign_arn,
            'Reason': reason
        }),
        resources = [ campaign_arn ]
    )

    put_event(
        detail_type = 'BuildPersonalizeMonitorDashboard',
        detail = json.dumps({
            'CampaignARN': campaign_arn,
            'Reason': reason
        }),
        resources = [ campaign_arn ]
    )

    logger.info({
        'campaignArn': campaign_arn
    })

    delete_alarms_for_campaign(campaign_arn)
    
    return f'Successfully initiated delete of campaign {campaign_arn}'